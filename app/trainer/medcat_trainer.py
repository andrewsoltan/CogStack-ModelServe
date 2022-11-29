import gc
import logging
import os
import re
import shutil
import ijson
from contextlib import redirect_stdout
from typing import TextIO, Dict, Optional

import pandas as pd
from medcat import __version__ as medcat_version
from medcat.cat import CAT
from management.log_captor import LogCaptor
from management.model_manager import ModelManager
from model_services.base import AbstractModelService
from trainer.base import SupervisedTrainer, UnsupervisedTrainer
from processors.data_batcher import mini_batch
from processors.metrics_collector import evaluate_model_with_trainer_export, get_cui_counts_from_trainer_export

logger = logging.getLogger(__name__)


class _MedcatTrainerCommon(object):

    @staticmethod
    def get_flattened_config(model: CAT) -> Dict:
        params = {}
        for key, val in model.cdb.config.general.__dict__.items():
            params[f"general.{key}"] = str(val)
        for key, val in model.cdb.config.cdb_maker.__dict__.items():
            params[f"cdb_maker.{key}"] = str(val)
        for key, val in model.cdb.config.annotation_output.__dict__.items():
            params[f"annotation_output.{key}"] = str(val)
        for key, val in model.cdb.config.preprocessing.__dict__.items():
            params[f"preprocessing.{key}"] = str(val)
        for key, val in model.cdb.config.ner.__dict__.items():
            params[f"ner.{key}"] = str(val)
        for key, val in model.cdb.config.linking.__dict__.items():
            params[f"linking.{key}"] = str(val)
        params["word_skipper"] = str(model.cdb.config.word_skipper)
        params["punct_checker"] = str(model.cdb.config.punct_checker)
        params.pop("linking.filters", None)  # deal with the length value in the older model
        for key, val in params.items():
            if val == "":  # otherwise it will trigger an MLflow bug
                params[key] = "<EMPTY>"
        return params

    @staticmethod
    def deploy_model(model_service: AbstractModelService,
                     model: CAT,
                     skip_save_model: bool) -> None:
        if skip_save_model:
            model._versioning()
        del model_service.model
        gc.collect()
        model_service.model = model
        logger.info("Retrained model deployed")

    @staticmethod
    def housekeep_file(file_path: Optional[str]) -> None:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.debug("model pack housekept")
        if file_path and os.path.exists(file_path.replace(".zip", "")):
            shutil.rmtree(file_path.replace(".zip", ""))
            logger.debug("Unpacked model directory housekept")

    @staticmethod
    def save_model(model: CAT, retrained_models_dir: str) -> str:
        logger.info(f"Saving retrained model to {retrained_models_dir}...")
        model_pack_name = model.create_model_pack(retrained_models_dir, "model")
        model_pack_path = f"{os.path.join(retrained_models_dir, model_pack_name)}.zip"
        logger.debug(f"Retrained model saved to {model_pack_path}")
        return model_pack_path


class MedcatSupervisedTrainer(SupervisedTrainer, _MedcatTrainerCommon):

    def __init__(self, model_service: AbstractModelService) -> None:
        SupervisedTrainer.__init__(self, model_service._config, model_service.model_name)
        self._model_service = model_service
        self._model_name = model_service.model_name
        self._model_pack_path = model_service._model_pack_path
        self._retrained_models_dir = os.path.join(model_service._model_parent_dir, "retrained")
        self._meta_cat_config_dict = model_service._meta_cat_config_dict
        self._model_manager = ModelManager(type(self), model_service._config)

    @staticmethod
    def run(trainer: SupervisedTrainer,
            training_params: Dict,
            data_file: TextIO,
            log_frequency: int,
            run_id: str) -> None:
        training_params.update({"print_stats": log_frequency})
        model_pack_path = None
        cdb_config_path = None
        copied_model_pack_path = None
        redeploy = trainer._config.REDEPLOY_TRAINED_MODEL == "true"
        skip_save_model = trainer._config.SKIP_SAVE_MODEL == "true"
        try:
            logger.info("Loading a new model copy for training...")
            copied_model_pack_path = trainer._make_model_file_copy(trainer._model_pack_path)
            model = trainer._model_service.load_model(copied_model_pack_path, meta_cat_config_dict=trainer._meta_cat_config_dict)
            trainer._tracker_client.log_model_config(trainer.get_flattened_config(model))
            logger.info("Performing supervised training...")
            with redirect_stdout(LogCaptor(trainer._glean_and_log_metrics)):
                fps, fns, tps, p, r, f1, cc, _ = model.train_supervised(**training_params)
            del _
            gc.collect()
            cuis = []
            f1 = {c: f for c, f in sorted(f1.items(), key=lambda item: item[0])}
            fp_accumulated = 0
            fn_accumulated = 0
            tp_accumulated = 0
            cc_accumulated = 0
            aggregated_metrics = []
            for cui, f1_val in f1.items():
                fp_accumulated += fps.get(cui, 0)
                fn_accumulated += fns.get(cui, 0)
                tp_accumulated += tps.get(cui, 0)
                cc_accumulated += cc.get(cui, 0)
                aggregated_metrics.append({
                    "per_concept_fp": fps.get(cui, 0),
                    "per_concept_fn": fns.get(cui, 0),
                    "per_concept_tp": tps.get(cui, 0),
                    "per_concept_counts": cc.get(cui, 0),
                    "per_concept_count_train": model.cdb.cui2count_train.get(cui, 0),
                    "per_concept_acc_fp": fp_accumulated,
                    "per_concept_acc_fn": fn_accumulated,
                    "per_concept_acc_tp": tp_accumulated,
                    "per_concept_acc_cc": cc_accumulated,
                    "per_concept_precision": p[cui],
                    "per_concept_recall": r[cui],
                    "per_concept_f1": f1_val,
                })
                cuis.append(cui)
            trainer._tracker_client.send_batched_model_stats(aggregated_metrics, run_id)
            cui_counts = get_cui_counts_from_trainer_export(data_file.name)
            trainer._save_trained_concepts(cui_counts, model)
            trainer._tracker_client.log_classes(cuis)
            trainer._evaluate_model_and_save_results(data_file.name, trainer._model_service.from_model(model))
            if not skip_save_model:
                model_pack_path = trainer.save_model(model, trainer._retrained_models_dir)
                cdb_config_path = model_pack_path.replace(".zip", "_config.json")
                model.cdb.config.save(cdb_config_path)
                trainer._tracker_client.save_model(model_pack_path, trainer._model_name, trainer._model_manager)
                trainer._tracker_client.save_model_artifact(cdb_config_path, trainer._model_name)
            else:
                logger.info("Skipped saving on the retrained model")
            if redeploy:
                trainer.deploy_model(trainer._model_service, model, skip_save_model)
            else:
                del model
                gc.collect()
                logger.info("Skipped deployment on the retrained model")
            logger.info("Supervised training finished")
            trainer._tracker_client.log_trainer_version(medcat_version)
            trainer._tracker_client.end_with_success()
        except Exception as e:
            logger.error("Supervised training failed")
            logger.error(e, exc_info=True, stack_info=True)
            trainer._tracker_client.log_exception(e)
            trainer._tracker_client.end_with_failure()
        finally:
            data_file.close()
            with trainer._training_lock:
                trainer._training_in_progress = False
            trainer.housekeep_file(model_pack_path)
            trainer.housekeep_file(copied_model_pack_path)
            if cdb_config_path:
                os.remove(cdb_config_path)

    def _glean_and_log_metrics(self, log: str) -> None:
        metric_lines = re.findall(r"Epoch: (\d+), Prec: (\d+\.\d+), Rec: (\d+\.\d+), F1: (\d+\.\d+)", log,
                                  re.IGNORECASE)
        for step, metric in enumerate(metric_lines):
            metrics = {
                "precision": float(metric[1]),
                "recall": float(metric[2]),
                "f1": float(metric[3]),
            }
            self._tracker_client.send_model_stats(metrics, int(metric[0]))

    def _save_trained_concepts(self, training_concepts: Dict, model: CAT) -> None:
        if len(training_concepts.keys()) != 0:
            unknown_concepts = set(training_concepts.keys()) - set(model.cdb.cui2names.keys())
            unknown_concept_pct = round(len(unknown_concepts) / len(training_concepts.keys()) * 100, 2)
            self._tracker_client.send_model_stats({
                "unknown_concept_count": len(unknown_concepts),
                "unknown_concept_pct": unknown_concept_pct,
            }, 0)
            if unknown_concepts:
                self._tracker_client.save_dataframe("unknown_concepts.csv",
                                                    pd.DataFrame({"concept": list(unknown_concepts)}),
                                                    self._model_name)
            train_count = []
            concept_names = []
            annotation_count = []
            concepts = list(training_concepts.keys())
            for c in concepts:
                train_count.append(model.cdb.cui2count_train[c] if c in model.cdb.cui2count_train else 0)
                concept_names.append(model.cdb.get_name(c))
                annotation_count.append(training_concepts[c])
            self._tracker_client.save_dataframe("trained_concepts.csv",
                                                pd.DataFrame({
                                                    "concept": concepts,
                                                    "name": concept_names,
                                                    "train_count": train_count,
                                                    "anno_count": annotation_count,
                                                }),
                                                self._model_name)

    def _evaluate_model_and_save_results(self, data_file_path: str, medcat_model: AbstractModelService) -> None:
        self._tracker_client.save_dataframe("evaluation.csv",
                                            evaluate_model_with_trainer_export(data_file_path, medcat_model,  return_df=True),
                                            self._model_name)


class MedcatUnsupervisedTrainer(UnsupervisedTrainer, _MedcatTrainerCommon):

    def __init__(self, model_service: AbstractModelService) -> None:
        UnsupervisedTrainer.__init__(self, model_service._config, model_service.model_name)
        self._model_service = model_service
        self._model_name = model_service.model_name
        self._model_pack_path = model_service._model_pack_path
        self._retrained_models_dir = os.path.join(model_service._model_parent_dir, "retrained")
        self._meta_cat_config_dict = model_service._meta_cat_config_dict
        self._model_manager = ModelManager(type(self), model_service._config)

    @staticmethod
    def run(trainer: UnsupervisedTrainer,
            training_params: Dict,
            data_file: TextIO,
            log_frequency: int,
            run_id: str) -> None:
        model_pack_path = None
        cdb_config_path = None
        copied_model_pack_path = None
        redeploy = trainer._config.REDEPLOY_TRAINED_MODEL == "true"
        skip_save_model = trainer._config.SKIP_SAVE_MODEL == "true"
        data_file.seek(0)
        texts = ijson.items(data_file, "item")
        try:
            logger.info("Loading a new model copy for training...")
            copied_model_pack_path = trainer._make_model_file_copy(trainer._model_pack_path)
            model = trainer._model_service.load_model(copied_model_pack_path, meta_cat_config_dict=trainer._meta_cat_config_dict)
            trainer._tracker_client.log_model_config(trainer.get_flattened_config(model))
            logger.info("Performing unsupervised training...")
            step = 0
            trainer._tracker_client.send_model_stats(model.cdb.make_stats(), step)
            before_cui2count_train = dict(model.cdb.cui2count_train)
            for batch in mini_batch(texts, batch_size=log_frequency):
                step += 1
                model.train(batch, **training_params)
                trainer._tracker_client.send_model_stats(model.cdb.make_stats(), step)
            after_cui2count_train = {c: ct for c, ct in
                                     sorted(model.cdb.cui2count_train.items(), key=lambda item: item[1], reverse=True)}
            aggregated_metrics = []
            cui_step = 0
            for cui, train_count in after_cui2count_train.items():
                if cui_step >= 10000:  # large numbers will cause the mlflow page to hung on loading
                    break
                cui_step += 1
                aggregated_metrics.append({
                    "per_concept_train_count_before": before_cui2count_train.get(cui, 0),
                    "per_concept_train_count_after": train_count
                })
            trainer._tracker_client.send_batched_model_stats(aggregated_metrics, run_id)
            if not skip_save_model:
                model_pack_path = trainer.save_model(model, trainer._retrained_models_dir)
                cdb_config_path = model_pack_path.replace(".zip", "_config.json")
                model.cdb.config.save(cdb_config_path)
                trainer._tracker_client.save_model(model_pack_path, trainer._model_name, trainer._model_manager)
                trainer._tracker_client.save_model_artifact(cdb_config_path, trainer._model_name)
            else:
                logger.info("Skipped saving on the retrained model")
            if redeploy:
                trainer.deploy_model(trainer._model_service, model, skip_save_model)
            else:
                del model
                gc.collect()
                logger.info("Skipped deployment on the retrained model")
            logger.info("Unsupervised training finished")
            trainer._tracker_client.log_trainer_version(medcat_version)
            trainer._tracker_client.end_with_success()
        except Exception as e:
            logger.error("Unsupervised training failed")
            logger.error(e, exc_info=True, stack_info=True)
            trainer._tracker_client.log_exception(e)
            trainer._tracker_client.end_with_failure()
        finally:
            data_file.close()
            with trainer._training_lock:
                trainer._training_in_progress = False
            trainer.housekeep_file(model_pack_path)
            trainer.housekeep_file(copied_model_pack_path)
            if cdb_config_path:
                os.remove(cdb_config_path)