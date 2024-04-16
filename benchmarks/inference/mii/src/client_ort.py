# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

import argparse
import asyncio
import json
import multiprocessing
import os
import queue
import random
import requests
import threading
import time
from typing import List, Iterable, Union

import numpy as np
from transformers import AutoTokenizer

try:
    from .postprocess_results import ResponseDetails
    from .random_query_generator import RandomQueryGenerator
    from .sample_input import all_text
    from .utils import parse_args, print_summary, get_args_product, CLIENT_PARAMS
except ImportError:
    from postprocess_results import ResponseDetails
    from random_query_generator import RandomQueryGenerator
    from sample_input import all_text
    from utils import parse_args, print_summary, get_args_product, CLIENT_PARAMS



def call_vllm(
    input_tokens: str, max_new_tokens: int, args: argparse.Namespace
) -> ResponseDetails:
    if not args.stream:
        raise NotImplementedError("Not implemented for non-streaming")

    api_url = "http://localhost:8000/generate"

    headers = {
        "User-Agent": "Benchmark Client",
        "Accept": "text/event-stream",
        "Content-Type": "application/json"
    }

    pload = {
        "prompt": input_tokens,
        "n": 1,
        "use_beam_search": False,
        "temperature": 1.0,
        "top_p": 0.9,
        "max_tokens": max_new_tokens,
        "ignore_eos": False,
        "stream": args.stream,
    }

    def clear_line(n: int = 1) -> None:
        LINE_UP = "\033[1A"
        LINE_CLEAR = "\x1b[2K"
        for _ in range(n):
            print(LINE_UP, end=LINE_CLEAR, flush=True)

    def get_streaming_response(
        response: requests.Response, time_last_token
    ) -> Iterable[List[str]]:
        for chunk in response.iter_lines(
            chunk_size=8192, decode_unicode=False, delimiter=b"\0"
        ):
            if chunk:
                data = json.loads(chunk.decode("utf-8"))
                output = data["text"][0]
                time_now = time.time()
                yield output, time_now - time_last_token
                time_last_token = time_now

    # For non-streaming, but currently non-streaming is not fully implemented
    def get_response(response: requests.Response) -> List[str]:
        data = json.loads(response.content)
        output = data["text"]
        return output

    token_gen_time = []
    start_time = time.time()
    response = requests.post(api_url, headers=headers, json=pload, stream=args.stream)
    for h, t in get_streaming_response(response, start_time):
        output = h
        token_gen_time.append(t)

    return ResponseDetails(
        generated_tokens=output,
        prompt=input_tokens,
        start_time=start_time,
        end_time=time.time(),
        model_time=0,
        token_gen_time=token_gen_time,
    )


def _run_parallel(
    barrier: Union[threading.Barrier, multiprocessing.Barrier],
    query_queue: Union[queue.Queue, multiprocessing.Queue],
    result_queue: Union[queue.Queue, multiprocessing.Queue],
    args: argparse.Namespace,
):
    pid = os.getpid()
    session_id = f"test_session_p{pid}_t{threading.get_ident()}"

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    backend_call_fns = {"fastgen": call_fastgen, "vllm": call_vllm, "aml": call_aml}
    call_fn = backend_call_fns[args.backend]

    barrier.wait()

    for _ in range(args.warmup):
        print(f"warmup queue size: {query_queue.qsize()} ({pid})", flush=True)
        input_tokens, req_max_new_tokens = query_queue.get(timeout=1.0)
        _ = call_fn(input_tokens, req_max_new_tokens, args)

    barrier.wait()

    time.sleep(random.uniform(0, args.num_clients) * 0.01)
    try:
        while not query_queue.empty():
            print(f"queue size: {query_queue.qsize()} ({pid})", flush=True)
            input_tokens, req_max_new_tokens = query_queue.get(timeout=1.0)

            r = call_fn(input_tokens, req_max_new_tokens, args)

            result_queue.put(r)
    except queue.Empty:
        print(f"queue is empty ({pid})")

    print(f"Worker ({pid}) finished. session_id: {session_id}")


def run_client(args):
    """
    Run MII client for benchmarking. The scenario is a bit complicated:
    1. The main process puts `num_requests` queries into the input queue
    2. Each client runs `warmup` iterations () taking the queries from the input queue
    3. --- barrier ---
    4. The main process marks the start time
    5a. All clients send `num_requests' query in total and put the results into the result queue
    5b. The main process takes the results from the result queue (in parallel with 5a)
    6. The main process marks the end time after receiving `num_requests' results
    """

    if args.use_thread:
        runnable_cls = threading.Thread
        barrier_cls = threading.Barrier
        queue_cls = queue.Queue
    else:
        runnable_cls = multiprocessing.Process
        barrier_cls = multiprocessing.Barrier
        queue_cls = multiprocessing.Queue

    barrier = barrier_cls(args.num_clients + 1)
    query_queue = queue_cls()
    result_queue = queue_cls()

    processes = [
        runnable_cls(
            target=_run_parallel,
            args=(
                barrier,
                query_queue,
                result_queue,
                args,
            ),
        )
        for i in range(args.num_clients)
    ]
    for p in processes:
        p.start()

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    query_generator = RandomQueryGenerator(all_text, tokenizer, seed=42)
    request_text = query_generator.get_random_request_text(
        args.mean_prompt_length,
        args.mean_prompt_length * args.prompt_length_var,
        args.max_prompt_length,
        args.num_requests + args.warmup * args.num_clients,
    )

    for t in request_text:
        # Set max_new_tokens following normal distribution
        req_max_new_tokens = int(
            np.random.normal(
                args.mean_max_new_tokens,
                args.max_new_tokens_var * args.mean_max_new_tokens,
            )
        )
        query_queue.put((t, req_max_new_tokens))

    # Tokenizers must be initialized after fork.
    # So we need to fork before putting inputs to the queue.
    # We need this barrier to stop child processse from taking inputs before the main process puts them
    barrier.wait()
    # This barrier is to make sure that all clients have finished warmup
    barrier.wait()

    response_details = []
    while len(response_details) < args.num_requests:
        res = result_queue.get()
        # vLLM returns concatinated tokens
        if args.backend == "vllm":
            all_tokens = tokenizer.tokenize(res.generated_tokens)
            res.generated_tokens = all_tokens[len(tokenizer.tokenize(res.prompt)) :]
        response_details.append(res)

    return response_details


if __name__ == "__main__":
    args = parse_args(client_args=True)

    for client_args in get_args_product(args, which=CLIENT_PARAMS):
        response_details = run_client(client_args)

        print_summary(client_args, response_details)
