from typing import Type, List
from modal import Secret, method, gpu, Stub

from shared.protocol import (
    CompletionResponse,
    ErrorPayload,
    ErrorResponse,
    Payload,
)

from pydantic import BaseModel


class GpuParams(BaseModel):
    model_dir: str
    gpu_count: int = 1
    gpu_memory: int = 20
    keep_warm: int | None = None


def create_gpu_model(stub: Stub, params: GpuParams) -> Type:
    @stub.cls(
        gpu=gpu.A100(count=params.gpu_count, memory=params.gpu_memory),
        secret=Secret.from_name("huggingface"),
        allow_concurrent_inputs=12,
        container_idle_timeout=600,
        keep_warm=params.keep_warm,
    )
    class Model:
        async def __aenter__(self):
            from vllm.engine.arg_utils import AsyncEngineArgs
            from vllm.engine.async_llm_engine import AsyncLLMEngine
            from vllm.transformers_utils.tokenizer import get_tokenizer

            engine_args = AsyncEngineArgs(
                model=params.model_dir,
                tensor_parallel_size=params.gpu_count,
                # using 95% of GPU memory by default
                gpu_memory_utilization=0.95,
                disable_log_requests=True,
                max_num_batched_tokens=4096,
            )

            self.engine = AsyncLLMEngine.from_engine_args(engine_args)

            self.engine_model_config = await self.engine.get_model_config()
            self.max_model_len = self.engine_model_config.get_max_model_len()

            # A separate tokenizer to map token IDs to strings.
            self.tokenizer = get_tokenizer(
                engine_args.tokenizer,
                tokenizer_mode=engine_args.tokenizer_mode,
                trust_remote_code=engine_args.trust_remote_code,
            )

        @method()
        async def tokenize_prompt(self, payload: Payload) -> List[int]:
            return self.tokenizer(payload.prompt).input_ids

        @method()
        async def max_model_len(self) -> int:
            return self.max_model_len

        @method()
        async def generate(self, payload: Payload, params, input_ids):
            try:
                import time

                results_generator = self.engine.generate(
                    payload.prompt, params, payload.id, input_ids
                )

                t0 = time.time()
                index, tokens = 0, 0
                output = ""
                async for request_output in results_generator:
                    # Skipping invalid UTF8 tokens:
                    if (
                        request_output.outputs[0].text
                        and "\ufffd" == request_output.outputs[0].text[-1]
                    ):
                        continue
                    token = request_output.outputs[0].text[index:]
                    if payload.stream:
                        choice = CompletionResponse(text=token).json(
                            ensure_ascii=False
                        )
                        yield f"data: {choice}\n\n"
                    else:
                        output += token
                    index = len(request_output.outputs[0].text)
                    # Token accounting
                    tokens = len(request_output.outputs[0].token_ids)

                if not payload.stream:
                    yield CompletionResponse(text=output).json(
                        ensure_ascii=False
                    )

                throughput = tokens / (time.time() - t0)
                print(f"Tokens count: {tokens} tokens")
                print(f"Request completed: {throughput:.4f} tokens/s")

                # yield "[DONE]"
                # print(request_output.outputs[0].text)
            except Exception as err:
                error_response = ErrorResponse(
                    error=ErrorPayload(
                        message=f"{err}", type=f"{type(err).__name__}"
                    )
                ).json(ensure_ascii=False)

                if payload.stream:
                    yield f"data: {error_response}\n\n"
                else:
                    yield error_response

    return Model
