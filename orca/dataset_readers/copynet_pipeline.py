import json
import logging
from typing import List, Dict

import numpy as np
from overrides import overrides

from allennlp.common.checks import ConfigurationError
from allennlp.common.file_utils import cached_path
from allennlp.common.util import START_SYMBOL, END_SYMBOL
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.fields import TextField, ArrayField, MetadataField, NamespaceSwappingField
from allennlp.data.instance import Instance
from allennlp.data.tokenizers import Token, Tokenizer, WordTokenizer
from allennlp.data.token_indexers import TokenIndexer, SingleIdTokenIndexer
from allennlp.models.archival import load_archive
from allennlp.predictors import Predictor

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@DatasetReader.register("copynet_pipeline")
class CopyNetPipelineDatasetReader(DatasetReader):
    """
    Read a tsv file containing paired sequences, and create a dataset suitable for a
    ``CopyNet`` model, or any model with a matching API.

    The expected format for each input line is: <source_sequence_string><tab><target_sequence_string>.
    An instance produced by ``CopyNetDatasetReader`` will containing at least the following fields:

    - ``source_tokens``: a ``TextField`` containing the tokenized source sentence,
       including the ``START_SYMBOL`` and ``END_SYMBOL``.
       This will result in a tensor of shape ``(batch_size, source_length)``.

    - ``source_token_ids``: an ``ArrayField`` of size ``(batch_size, trimmed_source_length)``
      that contains an ID for each token in the source sentence. Tokens that
      match at the lowercase level will share the same ID. If ``target_tokens``
      is passed as well, these IDs will also correspond to the ``target_token_ids``
      field, i.e. any tokens that match at the lowercase level in both
      the source and target sentences will share the same ID. Note that these IDs
      have no correlation with the token indices from the corresponding
      vocabulary namespaces.

    - ``source_to_target``: a ``NamespaceSwappingField`` that keeps track of the index
      of the target token that matches each token in the source sentence.
      When there is no matching target token, the OOV index is used.
      This will result in a tensor of shape ``(batch_size, trimmed_source_length)``.

    - ``metadata``: a ``MetadataField`` which contains the source tokens and
      potentially target tokens as lists of strings.

    When ``target_string`` is passed, the instance will also contain these fields:

    - ``target_tokens``: a ``TextField`` containing the tokenized target sentence,
      including the ``START_SYMBOL`` and ``END_SYMBOL``. This will result in
      a tensor of shape ``(batch_size, target_length)``.

    - ``target_token_ids``: an ``ArrayField`` of size ``(batch_size, target_length)``.
      This is calculated in the same way as ``source_token_ids``.

    See the "Notes" section below for a description of how these fields are used.

    Parameters
    ----------
    target_namespace : ``str``, required
        The vocab namespace for the targets. This needs to be passed to the dataset reader
        in order to construct the NamespaceSwappingField.
    source_tokenizer : ``Tokenizer``, optional
        Tokenizer to use to split the input sequences into words or other kinds of tokens. Defaults
        to ``WordTokenizer()``.
    target_tokenizer : ``Tokenizer``, optional
        Tokenizer to use to split the output sequences (during training) into words or other kinds
        of tokens. Defaults to ``source_tokenizer``.
    source_token_indexers : ``Dict[str, TokenIndexer]``, optional
        Indexers used to define input (source side) token representations. Defaults to
        ``{"tokens": SingleIdTokenIndexer()}``.
    target_token_indexers : ``Dict[str, TokenIndexer]``, optional
        Indexers used to define output (target side) token representations. Defaults to
        ``source_token_indexers``.

    Notes
    -----
    By ``source_length`` we are referring to the number of tokens in the source
    sentence including the ``START_SYMBOL`` and ``END_SYMBOL``, while
    ``trimmed_source_length`` refers to the number of tokens in the source sentence
    *excluding* the ``START_SYMBOL`` and ``END_SYMBOL``, i.e.
    ``trimmed_source_length = source_length - 2``.

    On the other hand, ``target_length`` is the number of tokens in the target sentence
    *including* the ``START_SYMBOL`` and ``END_SYMBOL``.

    In the context where there is a ``batch_size`` dimension, the above refer
    to the maximum of their individual values across the batch.

    In regards to the fields in an ``Instance`` produced by this dataset reader,
    ``source_token_ids`` and ``target_token_ids`` are primarily used during training
    to determine whether a target token is copied from a source token (or multiple matching
    source tokens), while ``source_to_target`` is primarily used during prediction
    to combine the copy scores of source tokens with the generation scores for matching
    tokens in the target namespace.
    """

    def __init__(self,
                 target_namespace: str,
                 span_predictor_model, 
                 source_tokenizer: Tokenizer = None,
                 target_tokenizer: Tokenizer = None,
                 source_token_indexers: Dict[str, TokenIndexer] = None,
                 lazy: bool = False,
                 add_rule = True,
                 embed_span = True,
                 add_question = True,
                 add_followup_ques = True,
                 train_using_gold = True)-> None:
        super().__init__(lazy)
        self._target_namespace = target_namespace
        self._source_tokenizer = source_tokenizer or WordTokenizer()
        self._target_tokenizer = target_tokenizer or self._source_tokenizer
        self._source_token_indexers = source_token_indexers or {"tokens": SingleIdTokenIndexer()}
        self.add_rule = add_rule
        self.embed_span = embed_span
        self.add_question = add_question
        self.add_followup_ques = add_followup_ques
        self.train_using_gold = train_using_gold
        if "tokens" not in self._source_token_indexers or \
                not isinstance(self._source_token_indexers["tokens"], SingleIdTokenIndexer):
            raise ConfigurationError("CopyNetDatasetReader expects 'source_token_indexers' to contain "
                                     "a 'single_id' token indexer called 'tokens'.")
        self._target_token_indexers: Dict[str, TokenIndexer] = {
                "tokens": SingleIdTokenIndexer(namespace=self._target_namespace)
        }

        archive = load_archive(span_predictor_model)
        self.dataset_reader = DatasetReader.from_params(archive.config.duplicate()["dataset_reader"])
        self.span_predictor = Predictor.from_archive(archive, 'sharc_predictor')

    @overrides
    def _read(self, file_path: str):
        # if `file_path` is a URL, redirect to the cache
        file_path = cached_path(file_path)

        logger.info("Reading file at %s", file_path)
        with open(file_path) as dataset_file:
            dataset = json.load(dataset_file)
        logger.info("Reading the dataset")
        for utterance in dataset:
            utterance_id = utterance['utterance_id']
            tree_id = utterance['tree_id']
            source_url = utterance['source_url']
            rule_text = utterance['snippet']
            question = utterance['question']
            scenario = utterance['scenario']
            history = utterance['history']

            if 'answer' in utterance.keys():
                answer = utterance['answer']
            if 'evidence' in utterance.keys():
                evidence = utterance['evidence']
            
            instance = self.text_to_instance(rule_text, question, scenario, history, answer, evidence)
            if instance is not None:
                yield instance

    @staticmethod
    def _tokens_to_ids(tokens: List[Token]) -> List[int]:
        ids: Dict[str, int] = {}
        out: List[int] = []
        for token in tokens:
            out.append(ids.setdefault(token.text.lower(), len(ids)))
        return out

    def get_prediction(self, rule_text, question, scenario, history):
        model_output = self.span_predictor.predict_json({'snippet': rule_text,
                                                         'question': question,
                                                         'scenario': scenario,
                                                         'history': history})
        predicted_span = model_output['best_span_str']
        predicted_label = model_output['label']
        return predicted_span, predicted_label

    def get_embedded_span(self, rule_text, predicted_span):
        start_index = rule_text.find(predicted_span)
        if start_index == -1:
            return rule_text
        else:
            output = rule_text[:start_index] + ' @pss@ ' + predicted_span + ' @pse@ '
            output += rule_text[start_index + len(predicted_span):]
            return output 

    @overrides
    def text_to_instance(self, rule_text, question, scenario, history, answer=None, evidence=None) -> Instance:  # type: ignore
        """
        Turn raw source string and target string into an ``Instance``.

        Parameters
        ----------
        source_string : ``str``, required
        target_string : ``str``, optional (default = None)

        Returns
        -------
        Instance
            See the above for a description of the fields that the instance will contain.
        """
        # pylint: disable=arguments-differ

        if answer and answer in ['Yes', 'No', 'Irrelevant']:
            return None
        target_string = answer

        if self.train_using_gold and answer is not None: # i.e. during training and validation
            predicted_label = answer if answer in ['Yes', 'No', 'Irrelevant'] else 'More'
            predicted_span_ixs = self.dataset_reader.find_lcs(rule_text, answer, self._source_tokenizer.tokenize)
            if predicted_span_ixs is None:
                return None
            else:
                rule_offsets = [(token.idx, token.idx + len(token.text)) for token in self._source_tokenizer.tokenize(rule_text)]
                predicted_span = rule_text[rule_offsets[predicted_span_ixs[0]][0]: rule_offsets[predicted_span_ixs[1]][1]]
        else:
            predicted_span, predicted_label = self.get_prediction(rule_text, question, scenario, history)

        if self.add_rule:
            if self.embed_span:
                source_string = self.get_embedded_span(rule_text, predicted_span)
            else:
                source_string = rule_text + ' @pss@ ' + predicted_span + ' @pse@'
        else:
            source_string = predicted_span
        if self.add_question:
            source_string += ' @qs@ ' + question + ' @qe'
        if self.add_followup_ques:
            for follow_up_qna in history:
                source_string += ' @fs@ ' + follow_up_qna['follow_up_question'] + ' @fe'

        tokenized_source = self._source_tokenizer.tokenize(source_string)
        tokenized_source.insert(0, Token(START_SYMBOL))
        tokenized_source.append(Token(END_SYMBOL))
        source_field = TextField(tokenized_source, self._source_token_indexers)

        # For each token in the source sentence, we keep track of the matching token
        # in the target sentence (which will be the OOV symbol if there is no match).
        source_to_target_field = NamespaceSwappingField(tokenized_source[1:-1], self._target_namespace)

        meta_fields = {"source_tokens": [x.text for x in tokenized_source[1:-1]]}
        fields_dict = {
                "source_tokens": source_field,
                "source_to_target": source_to_target_field,
        }

        if target_string is not None:
            tokenized_target = self._target_tokenizer.tokenize(target_string)
            tokenized_target.insert(0, Token(START_SYMBOL))
            tokenized_target.append(Token(END_SYMBOL))
            target_field = TextField(tokenized_target, self._target_token_indexers)

            fields_dict["target_tokens"] = target_field
            meta_fields["target_tokens"] = [y.text for y in tokenized_target[1:-1]]
            source_and_target_token_ids = self._tokens_to_ids(tokenized_source[1:-1] +
                                                              tokenized_target)
            source_token_ids = source_and_target_token_ids[:len(tokenized_source)-2]
            fields_dict["source_token_ids"] = ArrayField(np.array(source_token_ids))
            target_token_ids = source_and_target_token_ids[len(tokenized_source)-2:]
            fields_dict["target_token_ids"] = ArrayField(np.array(target_token_ids))
        else:
            source_token_ids = self._tokens_to_ids(tokenized_source[1:-1])
            fields_dict["source_token_ids"] = ArrayField(np.array(source_token_ids))

        meta_fields['label'] = predicted_label
        fields_dict["metadata"] = MetadataField(meta_fields)

        return Instance(fields_dict)
