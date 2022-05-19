import os
import shutil
import logging
import torch
import numpy as np
from typing import Tuple, List, Dict
from scipy.special import softmax
from transformers import AutoModelForTokenClassification, Trainer, PreTrainedModel
from medcat.tokenizers.tokenizer_ner import TokenizerNER
from model_services.base import AbstractModelService
from domain import ModelCard
from config import Settings

logger = logging.getLogger(__name__)


class DeIdModel(AbstractModelService):

    def __init__(self, config: Settings) -> None:
        self.config = config
        model_file_path = os.path.join(os.path.dirname(__file__), "..", "model", config.BASE_MODEL_FILE)
        self.tokenizer, self.model = self.load_model(model_file_path)
        self.id2cui = {cui_id: cui for cui, cui_id in self.tokenizer.label_map.items()}
        if config.DEVICE.startswith("cuda") and not torch.cuda.is_available():
            logger.warning("Service is configured to using GPUs but no GPUs were found.")
            self.device = "cpu"
        else:
            self.device = config.DEVICE
        self.model.to(self.device)
        self.trainer = Trainer(model=self.model, tokenizer=None)

    @staticmethod
    def info() -> ModelCard:
        return ModelCard(model_description="De-identification model", model_type="medcat")

    @staticmethod
    def load_model(model_file_path: str, *args, **kwargs) -> Tuple[TokenizerNER, PreTrainedModel]:
        model_file_dir = os.path.dirname(model_file_path)
        model_file_name = os.path.basename(model_file_path).replace(".zip", "")
        unpacked_model_dir = os.path.join(model_file_dir, model_file_name)
        if not os.path.isdir(unpacked_model_dir):
            shutil.unpack_archive(model_file_path, extract_dir=unpacked_model_dir)
        tokenizer_path = os.path.join(unpacked_model_dir, "tokenizer.dat")
        tokenizer = TokenizerNER.load(tokenizer_path)
        logger.info(f"Tokenizer loaded from {tokenizer_path}")
        model = AutoModelForTokenClassification.from_pretrained(unpacked_model_dir)
        logger.info(f"Model loaded from {unpacked_model_dir}")
        return tokenizer, model

    def annotate(self, text: str) -> List[Dict]:
        return self._get_annotations(text)

    def batch_annotate(self, texts: List[str]) -> List[List[Dict]]:
        annotation_list = []
        for text in texts:
            annotation_list.append(self._get_annotations(text))
        return annotation_list

    def _get_annotations(self, text: str) -> List[Dict]:
        if not text.strip():
            return []
        self.model.eval()
        dataset, offset_mappings = self._get_chunked_tokens(text)
        prediction_output = self.trainer.predict(dataset)   # type: ignore
        predictions = np.array(prediction_output.predictions)
        predictions = softmax(predictions, axis=2)
        batched_cui_ids = np.argmax(predictions, axis=2)
        annotations: List[Dict] = []

        for ps_idx, cui_ids in enumerate(batched_cui_ids):
            input_ids = dataset[ps_idx]["input_ids"]
            for t_idx, cur_cui_id in enumerate(cui_ids):
                if cur_cui_id not in [0, -100]:
                    t_text = self.tokenizer.hf_tokenizer.decode(input_ids[t_idx].item())
                    if t_text.strip() in ["", "[PAD]"]:
                        continue
                    annotation = {
                        "label_name": self.tokenizer.cui2name.get(self.id2cui[cur_cui_id]),
                        "label_id": self.id2cui[cur_cui_id],
                        "start": offset_mappings[ps_idx][t_idx][0],
                        "end": offset_mappings[ps_idx][t_idx][1],
                    }
                    if self.config.INCLUDE_ANNOTATION_TEXT == "true":
                        annotation["text"] = t_text
                    if annotations:
                        token_type = self.tokenizer.id2type.get(input_ids[t_idx].item())
                        if (self._should_expand_with_partial(cur_cui_id, token_type, annotation, annotations) or
                            self._should_expand_with_whole(annotation, annotations)):
                            annotations[-1]["end"] = annotation["end"]
                            if self.config.INCLUDE_ANNOTATION_TEXT == "true":
                                annotations[-1]["text"] = text[annotations[-1]["start"]:annotations[-1]["end"]]
                            del annotation
                            continue
                        elif cur_cui_id != 1:
                            annotations.append(annotation)
                            continue
                    else:
                        if cur_cui_id != 1:
                            annotations.append(annotation)
                            continue
        return annotations

    def _get_chunked_tokens(self, text: str) -> Tuple[List[Dict], List[Tuple]]:
        tokens = self.tokenizer.hf_tokenizer(text, return_offsets_mapping=True, add_special_tokens=False)
        model_max_length = self.tokenizer.max_len
        pad_token_id = self.tokenizer.hf_tokenizer.pad_token_id
        dataset = []
        offset_mappings = []
        for i in range(0, len(tokens["input_ids"]), model_max_length):
            dataset.append({
                "input_ids": torch.tensor(tokens["input_ids"][i:i+model_max_length]).to(self.device),
                "attention_mask": torch.tensor(tokens["attention_mask"][i:i+model_max_length]).to(self.device),
            })
            offset_mappings.append(tokens["offset_mapping"][i:i+model_max_length])
        remainder = len(tokens["input_ids"]) % model_max_length
        if remainder and i >= model_max_length:
            del dataset[-1]
            del offset_mappings[-1]
            dataset.append({
                "input_ids": torch.tensor(tokens["input_ids"][-remainder:] + [pad_token_id]*(model_max_length-remainder)).to(self.device),
                "attention_mask": torch.tensor(tokens["attention_mask"][-remainder:] + [0]*(model_max_length-remainder)).to(self.device),
            })
            offset_mappings.append(tokens["offset_mapping"][-remainder:] +
                [(tokens["offset_mapping"][-1][1]+i, tokens["offset_mapping"][-1][1]+i+1) for i in range(model_max_length-remainder)])
        del tokens
        return dataset, offset_mappings

    @staticmethod
    def _should_expand_with_partial(cur_cui_id: int,
                                    cur_token_type: str,
                                    annotation: Dict,
                                    annotations: List[Dict]) -> bool:
        return all([cur_cui_id == 1, cur_token_type == "sub", (annotation["start"] - annotations[-1]["end"]) in [0, 1]])

    @staticmethod
    def _should_expand_with_whole(annotation: Dict, annotations: List[Dict]) -> bool:
        return annotation["label_id"] == annotations[-1]["label_id"] and (annotation["start"] - annotations[-1]["end"]) in [0, 1]
