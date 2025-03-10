# -*- coding: utf-8 -*-
# Copyright (C) 2018-2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import logging
from dataclasses import dataclass, field, fields
from functools import lru_cache
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np
import openvino
from openvino import Model, Output, Tensor, Type
from openvino import opset12 as opset
from openvino.op import Constant
from openvino.preprocess import PrePostProcessor

from .__version__ import __version__ as openvino_tokenizers_version
from .constants import (
    LOGITS_OUTPUT_NAME,
    ORIGINAL_TOKENIZER_CLASS_NAME,
    SPACE_SYMBOLS,
    TOKEN_IDS_OUTPUT_NAME,
    UTF8ReplaceMode,
    rt_info_to_hf_attribute_map,
)


@dataclass
class TokenzierConversionParams:
    """
    with_detokenizer : bool
        Whether to include a detokenizer in the conversion process. Default is False.

    add_special_tokens : bool
        Whether to add special tokens during tokenization. Default is True.

    skip_special_tokens : bool
        Whether to skip special tokens during detokenization. Default is True.

    clean_up_tokenization_spaces : Optional[bool]
        If True, extra spaces will be cleaned up during the tokenization process. Default is None.

    tokenizer_output_type : Type
        The output type for the tokenizer model. Default is `Type.i64`.

    detokenizer_input_type : Type
        The input type for the detokenizer model. Default is `Type.i64`.

    streaming_detokenizer : bool
        If True, enables streaming mode for the detokenizer. Default is False.

    use_max_padding : bool
        If True, enables maximum padding for the tokenizer. Default is False.

    max_length: Optional[int]
        The maximum length of the input sequence.

    handle_special_tokens_with_re : Optional[bool]
        If True, uses regular expressions to handle special tokens during tokenization. Default is None.

    use_sentencepiece_backend : bool
        If True, forces the use of the SentencePiece backend during tokenization. Default is False.

    utf8_replace_mode : Optional[UTF8ReplaceMode]
        Specifies the UTF-8 replacement mode during tokenization.
        Allowed values are UTF8ReplaceMode.DISABLE, UTF8ReplaceMode.IGNORE and UTF8ReplaceMode.REPLACE. Default is UTF8ReplaceMode.REPLACE.
    number_of_inputs: int
        The number of inputs for the model. Default is 1.
    """

    with_detokenizer: bool = False
    add_special_tokens: bool = True
    skip_special_tokens: bool = True
    clean_up_tokenization_spaces: Optional[bool] = None
    tokenizer_output_type: Type = Type.i64
    detokenizer_input_type: Type = Type.i64
    streaming_detokenizer: bool = False
    use_max_padding: bool = False
    max_length: Optional[int] = None
    handle_special_tokens_with_re: Optional[bool] = None
    use_sentencepiece_backend: bool = False
    utf8_replace_mode: Optional[UTF8ReplaceMode] = field(default_factory=lambda: UTF8ReplaceMode.REPLACE)
    add_attention_mask: bool = True
    add_prefix_space: Optional[bool] = None
    number_of_inputs: int = 1


logger = logging.getLogger(__name__)


def connect_models(
    first: Model,
    second: Model,
    name_map: Optional[Union[Sequence[Tuple[str, str]], Dict[str, str]]] = None,
    by_indices: bool = False,
    keep_second_model_unaligned_inputs: bool = True,
    keep_remaining_first_model_outputs: bool = False,
) -> Model:
    if by_indices:
        min_len = min(len(first.outputs), len(second.inputs))
        aligned_first_outputs = first.outputs[:min_len]
        aligned_second_inputs = second.inputs[:min_len]
    elif name_map is None:
        aligned_first_outputs = first.outputs
        aligned_second_inputs = [second.input(model1_output.get_any_name()) for model1_output in aligned_first_outputs]
    else:
        if isinstance(name_map, dict):
            name_map = list(name_map.items())
        aligned_first_outputs = [first.output(name1) for name1, _ in name_map]
        aligned_second_inputs = [second.input(name2) for _, name2 in name_map]

    for second_input, first_output in zip(aligned_second_inputs, aligned_first_outputs):
        logger.debug(f"Connecting: {first_output.get_any_name()} -> {second_input.get_any_name()}")
        for target in second_input.get_target_inputs():
            target.replace_source_output(first_output.get_node().input_value(0))
            # target.replace_source_output(model1_output)  # TODO: Produces incorrect topology

    new_inputs = first.inputs
    remaining_inputs = [input_ for input_ in second.inputs if input_ not in aligned_second_inputs]
    if keep_second_model_unaligned_inputs:
        new_inputs.extend(remaining_inputs)
    elif remaining_inputs:
        logger.info(
            "Some inputs of the second model were left uncovered and not included in the connected model: "
            + ", ".join(input_.name for input_ in remaining_inputs)
            + ". To add them set `keep_unaligned_inputs` to `True`"
        )
    new_inputs = [input_.get_node() for input_ in new_inputs]

    new_outputs = second.outputs
    remaining_outputs = [output for output in first.outputs if output not in aligned_first_outputs]
    if keep_remaining_first_model_outputs:
        new_outputs.extend(remaining_outputs)
    elif remaining_outputs:
        logger.info(
            "Some outputs of the first model were left uncovered and not included in the connected model: "
            + ", ".join(output.name for output in remaining_outputs)
            + ". To add them set `keep_unaligned_outputs` to `True`"
        )

    connected_model = Model(new_outputs, new_inputs, f"{first.get_name()}_with_{second.get_name()}")
    # TODO: Cleanup model1 and mode2 to avoid using them, they are ill-formed after the reconnection
    connected_model.validate_nodes_and_infer_types()
    return connected_model


def greedy_decoder(input) -> Model:
    argmax = opset.topk(
        data=input,
        k=1,
        axis=-1,
        mode="max",
        sort="none",
        name="ArgMax",
    )
    token_ids = opset.squeeze(
        data=argmax.output(1),
        axes=-1,
    )
    return token_ids.output(0)


def add_greedy_decoding(
    text_generation_model: Model, logits_output: str = LOGITS_OUTPUT_NAME, output_type: Type = Type.i64
) -> Model:
    ppp = PrePostProcessor(text_generation_model)
    ppp.output(logits_output).postprocess().custom(greedy_decoder)
    ppp.output(logits_output).tensor().set_element_type(output_type)
    model = ppp.build()
    model.output(logits_output).tensor.set_names({TOKEN_IDS_OUTPUT_NAME})
    return model


def change_inputs_type(model: Model, input_type: Type) -> Model:
    ppp = PrePostProcessor(model)
    for idx, _ in enumerate(model.inputs):
        ppp.input(idx).tensor().set_element_type(input_type)
    return ppp.build()


def change_outputs_type(model: Model, output_type: Type) -> Model:
    ppp = PrePostProcessor(model)
    for idx, _ in enumerate(model.outputs):
        ppp.output(idx).tensor().set_element_type(output_type)
    return ppp.build()


# from transformers.models.gpt2.tokenization_gpt2
@lru_cache()
def unicode_to_bytes() -> Dict[str, int]:
    bs = (
        list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    )
    cs = bs[:]
    n = 0
    for b in range(2**8):
        if b not in bs:
            bs.append(b)
            cs.append(2**8 + n)
            n += 1
    cs = (chr(n) for n in cs)
    return dict(zip(cs, bs))


def apply_unicode_to_bytes(token: str, return_corrupted_tokens: bool = False) -> bytes:
    bytes_encoder = unicode_to_bytes()
    try:
        return bytes(bytes_encoder[char] for char in token)
    except KeyError:
        # tokens that was not bytes-to-chars encoded
        # ModernBERT adds such tokens to the vocab directly, which is wrong, but we need to handle it
        if return_corrupted_tokens:
            return token.encode()
        return b""


def transform_unigram_token_to_bytes(token: str, byte_fallback: bool = False) -> bytes:
    token = token.replace("▁", " ")
    if byte_fallback and len(token) == 6 and token.startswith("<0x") and token.endswith(">"):
        return bytes.fromhex(token[3:5])
    return token.encode()


def get_hf_tokenizer_attribute(
    hf_tokenizer: "PreTrainedTokenizerBase",  # noqa
    attributes: Tuple[str],
) -> Any:
    return next((value for attr in attributes if (value := getattr(hf_tokenizer, attr, None)) is not None), None)


def get_package_version(name: str) -> str:
    import importlib.metadata as metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def update_rt_info_with_environment(ov_tokenizer: Model) -> None:
    """Adds package versions used for conversion to rt_info

    :param ov_tokenizer: Thes OpenVINO tokenizer model to update.
    :type ov_tokenizer: openvino.Model
    """
    ov_tokenizer.set_rt_info(openvino.get_version(), "openvino_version")
    ov_tokenizer.set_rt_info(openvino_tokenizers_version, "openvino_tokenizers_version")

    packages = ["transformers", "tiktoken", "sentencepiece", "tokenizers"]

    for name in packages:
        version = get_package_version(name)
        if version is not None:
            ov_tokenizer.set_rt_info(version, f"{name}_version")


def update_rt_info_with_params(
    ov_tokenizer: Model,
    hf_tokenizer: "PreTrainedTokenizerBase",  # noqa
    params: TokenzierConversionParams,
) -> None:
    """Updates the runtime information of the OpenVINO tokenizer model with the parameters and attributes of the HF.

    :param ov_tokenizer: The OpenVINO tokenizer model to update.
    :type ov_tokenizer: openvino.Model
    :param hf_tokenizer: The Huggingface tokenizer object.
    :type hf_tokenizer: transformers.tokenization_utils_fast.PreTrainedTokenizerBase
    :param params: The conversion parameters.
    :type params: TokenzierConversionParams
    """
    ov_tokenizer.set_rt_info(str(type(hf_tokenizer)), ORIGINAL_TOKENIZER_CLASS_NAME)

    for key in fields(params):
        v = getattr(params, key.name)
        v = str(v) if isinstance(v, bool) else v
        ov_tokenizer.set_rt_info(v, key.name)

    for rt_field_name, hf_attributes in rt_info_to_hf_attribute_map.items():
        attribute = get_hf_tokenizer_attribute(hf_tokenizer, hf_attributes)
        if attribute is not None:
            ov_tokenizer.set_rt_info(attribute, rt_field_name)


def generate_tokens_with_space_symbols(token: str, depth: int = 1):
    for symbol_1 in SPACE_SYMBOLS:
        new_token = token + symbol_1
        yield new_token
        if depth > 1:
            yield from generate_tokens_with_space_symbols(new_token, depth - 1)


def quote_meta(unquoted: Union[str, bytes]) -> str:
    if isinstance(unquoted, bytes):
        unquoted = unquoted.decode("latin1")
    symbols = []
    for char in unquoted:
        if not char.isalnum() and char not in ("_", "▁", "｜"):
            symbols.append("\\")
        symbols.append(char)
    return "".join(symbols)


def to_bytes(number: int) -> bytes:
    return number.to_bytes(4, "little")


def create_unpacked_string(strings: Iterable[str]) -> List[Output]:
    """
    Convert any list of strings to U8/1D numpy array with begins, ends, and chars
    """
    begins = BytesIO()
    ends = BytesIO()
    chars = BytesIO()
    offset = 0

    for string in strings:
        byte_string = string.encode("utf-8") if isinstance(string, str) else string
        length = len(byte_string)

        begins.write(to_bytes(offset))
        offset += length
        ends.write(to_bytes(offset))
        chars.write(byte_string)

    begins = np.frombuffer(begins.getvalue(), np.int32)
    ends = np.frombuffer(ends.getvalue(), np.int32)
    chars = np.frombuffer(chars.getvalue(), np.uint8)

    return [Constant(Tensor(x)).output(0) for x in [begins, ends, chars]]
