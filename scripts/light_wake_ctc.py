"""Benchmark adapter for the installed upstream FunASR CTC prefix decoder.

Executes the unchanged upstream search algorithm with a NumPy tensor facade;
only the unused Torch import/type annotations are removed by AST. No decoder
algorithm is written here. Not a production provider or a runtime dependency.
"""

import ast
import hashlib
import importlib.metadata

import numpy as np


class Tensor:
    def __init__(self, values):
        self.values = np.asarray(values)

    def size(self, dim):
        return self.values.shape[dim]

    def __getitem__(self, index):
        value = self.values[index]
        return Tensor(value) if value.ndim else value

    def topk(self, count):
        index = np.argpartition(self.values, -count)[-count:]
        index = index[np.argsort(self.values[index])[::-1]]
        return self.values[index], index


def decoder_class():
    source_path = importlib.metadata.distribution("funasr").locate_file("funasr/utils/kws_utils.py")
    source = source_path.read_text()
    tree = ast.parse(source)
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + [
        node
        for node in tree.body
        if not (isinstance(node, ast.Import) and any(name.name == "torch" for name in node.names))
    ]
    ast.fix_missing_locations(tree)
    namespace = {}
    exec(compile(tree, str(source_path), "exec"), namespace)
    return namespace["KwsCtcPrefixDecoder"], hashlib.sha256(source.encode()).hexdigest()


def make_decoder(model_dir):
    cls, source_hash = decoder_class()
    token_file = model_dir / "funasr/tokens_2599.txt"
    tokens = token_file.read_text().splitlines()
    lexicon = {}
    # Retain just the explicitly requested symbols from the upstream lexicon.
    for line in (model_dir / "funasr/lexicon.txt").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] in ("嘟", "你", "好"):
            lexicon[fields[0]] = " ".join(fields[1:])
    decoder = cls(ctc=None, keywords="嘟嘟,你好嘟嘟", token_list=tokens, seg_dict=lexicon)
    for keyword in decoder.keywords_token.values():
        if any(tokens[i] in ("<unk>", "<blank>") for i in keyword["token_id"]):
            raise ValueError("Unsupported wake token")
    return decoder, source_hash


def score(decoder, probabilities):
    hit, word, probability = decoder._decode_inside(Tensor(probabilities), None)
    return float(probability) if hit else 0.0, word
