from typing import Type

import os

from fastapi import Depends, HTTPException, status
from modal import Secret, Stub, web_endpoint
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse

from shared.protocol import (
    create_error_response,
    Payload,
)

from shared.protocol import (
    Payload,
)

from pydantic import BaseModel


auth_scheme = HTTPBearer()


class CpuParams(BaseModel):
    keep_warm: int | None = None
    api_key_id: str


def create_cpu_completion_endpoint(
    stub: Stub, params: CpuParams, Model
) -> Type:
    @stub.function(
        secret=Secret.from_name("ext-api-key"),
        timeout=60 * 60,
        allow_concurrent_inputs=12,
        keep_warm=params.keep_warm,
    )
    @web_endpoint(method="POST")
    def completion(
        payload: Payload,
        token: HTTPAuthorizationCredentials = Depends(auth_scheme),
    ):
        if token.credentials != os.environ[params.api_key_id]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        from vllm.sampling_params import SamplingParams

        model = Model()

        max_model_len = model.max_model_len.remote()
        input_ids = model.tokenize_prompt.remote(payload)
        token_num = len(input_ids)

        if payload.params.max_tokens is None:
            max_tokens = max_model_len - token_num
        else:
            max_tokens = payload.params.max_tokens

        is_too_high = (token_num + max_tokens) > max_model_len

        if is_too_high:
            return create_error_response(
                status.HTTP_400_BAD_REQUEST,
                f"This model's maximum context length is {max_model_len} tokens. "
                f"However, you requested {max_tokens + token_num} tokens "
                f"({token_num} in the messages, "
                f"{max_tokens} in the completion). "
                f"Please reduce the length of the messages or completion.",
            )

        try:
            sampling_params = SamplingParams(
                # early_stopping=payload.params.early_stopping,
                # length_penalty=payload.params.length_penalty,
                best_of=payload.params.best_of,
                frequency_penalty=payload.params.frequency_penalty,
                ignore_eos=payload.params.ignore_eos,
                logprobs=payload.params.logprobs,
                max_tokens=max_tokens,
                n=payload.params.n,
                presence_penalty=payload.params.presence_penalty,
                stop=payload.params.stop,
                temperature=payload.params.temperature,
                top_k=payload.params.top_k,
                top_p=payload.params.top_p,
                use_beam_search=payload.params.use_beam_search,
            )
            print(sampling_params)
        except ValueError as e:
            return create_error_response(status.HTTP_400_BAD_REQUEST, str(e))

        return StreamingResponse(
            model.generate.remote_gen(payload, sampling_params, input_ids),
            media_type="text/event-stream",
        )

    return completion
