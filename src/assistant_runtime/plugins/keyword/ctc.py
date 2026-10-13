"""FunASR 1.4.16 CTC prefix search, MIT licensed (see FUNASR_LICENSE.txt).

Source: funasr/utils/kws_utils.py, SHA256
c4407ddb9d6c09b2cc56e464c920b338531271a251edefe4af3ad1902191c2b6.
Vendored inference-only: Torch import and unused model.decode wrapper removed;
annotations use object. Search/scoring algorithm unchanged. NumPy facade below
supplies the three tensor operations used by upstream search.
"""

import re
import numpy as np
import logging
import math
from collections import defaultdict
from typing import List, Tuple

symbol_str = "[’!\"#$%&'()*+,\\-./:;<>=?@，。?★、…【】《》？“”‘’！\\[\\]\\\\^_`{|}~\\s]+"


def split_mixed_label(input_str):
    """Split mixed label.

    Args:
        input_str: TODO.
    """
    tokens = []
    s = input_str.lower()
    while len(s) > 0:
        match = re.match("[A-Za-z!?,<>()\\']+", s)
        if match is not None:
            word = match.group(0)
        else:
            word = s[0:1]
        tokens.append(word)
        s = s.replace(word, "", 1).strip(" ")
    return tokens


def query_token_set(txt, symbol_table, lexicon_table):
    """Query token set.

    Args:
        txt: TODO.
        symbol_table: TODO.
        lexicon_table: TODO.
    """
    tokens_str = tuple()
    tokens_idx = tuple()
    if txt in symbol_table:
        tokens_str = tokens_str + (txt,)
        tokens_idx = tokens_idx + (symbol_table[txt],)
        return (tokens_str, tokens_idx)
    parts = split_mixed_label(txt)
    for part in parts:
        if part == "!sil" or part == "(sil)" or part == "<sil>":
            tokens_str = tokens_str + ("!sil",)
        elif part == "<blank>" or part == "<blank>":
            tokens_str = tokens_str + ("<blank>",)
        elif part == "(noise)" or part == "noise)" or part == "(noise" or (part == "<noise>"):
            tokens_str = tokens_str + ("<unk>",)
        elif part in symbol_table:
            tokens_str = tokens_str + (part,)
        elif part in lexicon_table:
            for ch in lexicon_table[part]:
                tokens_str = tokens_str + (ch,)
        else:
            part = re.sub(symbol_str, "", part)
            for ch in part:
                tokens_str = tokens_str + (ch,)
    for ch in tokens_str:
        if ch in symbol_table:
            tokens_idx = tokens_idx + (symbol_table[ch],)
        elif ch == "!sil":
            if "sil" in symbol_table:
                tokens_idx = tokens_idx + (symbol_table["sil"],)
            else:
                tokens_idx = tokens_idx + (symbol_table["<blank>"],)
        elif ch == "<unk>":
            if "<unk>" in symbol_table:
                tokens_idx = tokens_idx + (symbol_table["<unk>"],)
            else:
                tokens_idx = tokens_idx + (symbol_table["<blank>"],)
        elif "<unk>" in symbol_table:
            tokens_idx = tokens_idx + (symbol_table["<unk>"],)
            logging.info(f"'{ch}' is not in token set, replace with <unk>")
        else:
            tokens_idx = tokens_idx + (symbol_table["<blank>"],)
            logging.info(f"'{ch}' is not in token set, replace with <blank>")
    return (tokens_str, tokens_idx)


class KwsCtcPrefixDecoder:
    """Decoder interface wrapper for CTCPrefixDecode."""

    def __init__(self, ctc: object, keywords: str, token_list: list, seg_dict: dict):
        """Initialize class.

        Args:
            ctc (object): The CTC implementation.
                For example, :class:`espnet.nets.pytorch_backend.ctc.CTC`

        """
        self.ctc = ctc
        self.token_list = token_list
        token_table = {}
        for token in token_list:
            token_table[token] = token_list.index(token)
        self.keywords_idxset = {0}
        self.keywords_token = {}
        self.keywords_str = keywords
        keywords_list = self.keywords_str.strip().replace(" ", "").split(",")
        for keyword in keywords_list:
            strs, indexs = query_token_set(keyword, token_table, seg_dict)
            self.keywords_token[keyword] = {}
            self.keywords_token[keyword]["token_id"] = indexs
            self.keywords_token[keyword]["token_str"] = "".join(("%s " % str(i) for i in indexs))
            [self.keywords_idxset.add(i) for i in indexs]

    def beam_search(
        self,
        logits: object,
        logits_lengths: object,
        keywords_tokenset: set = None,
        score_beam_size: int = 3,
        path_beam_size: int = 20,
    ) -> Tuple[List[List[int]], object]:
        """CTC prefix beam search inner implementation

        Args:
            logits (object): (1, max_len, vocab_size)
            logits_lengths (object): (1, )
            keywords_tokenset (set): token set for filtering score
            score_beam_size (int): beam size for score
            path_beam_size (int): beam size for path

        Returns:
            List[List[int]]: nbest results
        """
        maxlen = logits.size(0)
        ctc_probs = logits
        cur_hyps = [(tuple(), (1.0, 0.0, []))]
        for t in range(0, maxlen):
            probs = ctc_probs[t]
            next_hyps = defaultdict(lambda: (0.0, 0.0, []))
            top_k_probs, top_k_index = probs.topk(score_beam_size)
            filter_probs = []
            filter_index = []
            for prob, idx in zip(top_k_probs.tolist(), top_k_index.tolist()):
                if keywords_tokenset is not None:
                    if prob > 0.05 and idx in keywords_tokenset:
                        filter_probs.append(prob)
                        filter_index.append(idx)
                elif prob > 0.05:
                    filter_probs.append(prob)
                    filter_index.append(idx)
            if len(filter_index) == 0:
                continue
            for s in filter_index:
                ps = probs[s].item()
                for prefix, (pb, pnb, cur_nodes) in cur_hyps:
                    last = prefix[-1] if len(prefix) > 0 else None
                    if s == 0:
                        n_pb, n_pnb, nodes = next_hyps[prefix]
                        n_pb = n_pb + pb * ps + pnb * ps
                        nodes = cur_nodes.copy()
                        next_hyps[prefix] = (n_pb, n_pnb, nodes)
                    elif s == last:
                        if not math.isclose(pnb, 0.0, abs_tol=1e-06):
                            n_pb, n_pnb, nodes = next_hyps[prefix]
                            n_pnb = n_pnb + pnb * ps
                            nodes = cur_nodes.copy()
                            if ps > nodes[-1]["prob"]:
                                nodes[-1]["prob"] = ps
                                nodes[-1]["frame"] = t
                            next_hyps[prefix] = (n_pb, n_pnb, nodes)
                        if not math.isclose(pb, 0.0, abs_tol=1e-06):
                            n_prefix = prefix + (s,)
                            n_pb, n_pnb, nodes = next_hyps[n_prefix]
                            n_pnb = n_pnb + pb * ps
                            nodes = cur_nodes.copy()
                            nodes.append(dict(token=s, frame=t, prob=ps))
                            next_hyps[n_prefix] = (n_pb, n_pnb, nodes)
                    else:
                        n_prefix = prefix + (s,)
                        n_pb, n_pnb, nodes = next_hyps[n_prefix]
                        if nodes:
                            if ps > nodes[-1]["prob"]:
                                nodes[-1]["prob"] = ps
                                nodes[-1]["frame"] = t
                        else:
                            nodes = cur_nodes.copy()
                            nodes.append(dict(token=s, frame=t, prob=ps))
                        n_pnb = n_pnb + pb * ps + pnb * ps
                        next_hyps[n_prefix] = (n_pb, n_pnb, nodes)
            next_hyps = sorted(next_hyps.items(), key=lambda x: x[1][0] + x[1][1], reverse=True)
            cur_hyps = next_hyps[:path_beam_size]
        hyps = [(y[0], y[1][0] + y[1][1], y[1][2]) for y in cur_hyps]
        return hyps

    def is_sublist(self, main_list, check_list):
        """Is sublist.

        Args:
            main_list: TODO.
            check_list: TODO.
        """
        if len(main_list) < len(check_list):
            return -1
        if len(main_list) == len(check_list):
            return 0 if main_list == check_list else -1
        for i in range(len(main_list) - len(check_list) + 1):
            if main_list[i] == check_list[0]:
                for j in range(len(check_list)):
                    if main_list[i + j] != check_list[j]:
                        break
                else:
                    return i
        else:
            return -1

    def _decode_inside(self, logits: object, logits_lengths: object):
        """Internal: decode inside.

        Args:
            logits: TODO.
            logits_lengths: Lengths of logits.
        """
        hyps = self.beam_search(logits, logits_lengths, self.keywords_idxset)
        hit_keyword = None
        hit_score = 1.0
        for one_hyp in hyps:
            prefix_ids = one_hyp[0]
            prefix_nodes = one_hyp[2]
            assert len(prefix_ids) == len(prefix_nodes)
            for word in self.keywords_token.keys():
                lab = self.keywords_token[word]["token_id"]
                offset = self.is_sublist(prefix_ids, lab)
                if offset != -1:
                    hit_keyword = word
                    for idx in range(offset, offset + len(lab)):
                        hit_score *= prefix_nodes[idx]["prob"]
                    break
            if hit_keyword is not None:
                hit_score = math.sqrt(hit_score)
                break
        if hit_keyword is not None:
            return (True, hit_keyword, hit_score)
        else:
            return (False, None, None)


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
