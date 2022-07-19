import os
import re
import socket
import mlflow
from typing import Dict, Tuple, List
from mlflow.utils.mlflow_tags import MLFLOW_SOURCE_NAME
from mlflow.entities import RunStatus
from monitoring.model_wrapper import ModelWrapper


class TrainingTracker(object):

    def __init__(self, mlflow_tracking_uri: str) -> None:
        mlflow.set_tracking_uri(mlflow_tracking_uri)

    @staticmethod
    def start_tracking(model_name: str,
                       input_file_name: str,
                       base_model_original: str,
                       training_type: str,
                       training_params: Dict,
                       training_id: str,
                       log_frequency: int) -> Tuple[str, str]:
        experiment_name = TrainingTracker._get_experiment_name(model_name, training_type)
        experiment_id = TrainingTracker._get_experiment_id(experiment_name)
        active_run = mlflow.start_run(experiment_id=experiment_id, run_name=training_id)
        mlflow.set_tags({
            MLFLOW_SOURCE_NAME: socket.gethostname(),
            "training.input_data.filename": input_file_name,
            "training.base_model.origin": base_model_original,
            "training.metrics.log_frequency": log_frequency,
        })
        mlflow.log_params(training_params)
        return experiment_id, active_run.info.run_id

    @staticmethod
    def end_with_success() -> None:
        mlflow.end_run(RunStatus.to_string(RunStatus.FINISHED))

    @staticmethod
    def end_with_failure() -> None:
        mlflow.end_run(RunStatus.to_string(RunStatus.FAILED))

    @staticmethod
    def end_with_interruption() -> None:
        mlflow.end_run(RunStatus.to_string(RunStatus.KILLED))

    @staticmethod
    def glean_and_log_metrics(log: str) -> None:
        metric_lines = re.findall(r"Epoch: (\d+), Prec: (\d+\.\d+), Rec: (\d+\.\d+), F1: (\d+\.\d+)", log, re.IGNORECASE)
        for step, metric in enumerate(metric_lines):
            metrics = {
                "precision": float(metric[1]),
                "recall": float(metric[2]),
                "f1": float(metric[3]),
            }
            mlflow.log_metrics(metrics, step)

    @staticmethod
    def send_model_stats(stats: Dict, step: int) -> None:
        metrics = {key.replace(" ", "_").lower(): val for key, val in stats.items()}
        mlflow.log_metrics(metrics, step)

    @staticmethod
    def save_model(filepath: str,
                   model_name: str,
                   pyfunc_model: ModelWrapper) -> None:
        model_name = model_name.replace(" ", "_")
        if not mlflow.get_tracking_uri().startswith("file:/"):
            mlflow.pyfunc.log_model(
                artifact_path=model_name,
                python_model=pyfunc_model,
                artifacts={"model_path": filepath},
                registered_model_name=model_name,
            )
        else:
            mlflow.pyfunc.log_model(
                artifact_path=model_name,
                python_model=pyfunc_model,
                artifacts={"model_path": filepath},
            )

    @staticmethod
    def save_model_artifact(filepath: str,
                            model_name: str) -> None:
        model_name = model_name.replace(" ", "_")
        mlflow.log_artifact(filepath, artifact_path=os.path.join(model_name, "artifacts"))

    @staticmethod
    def log_exception(e: Exception) -> None:
        mlflow.set_tag("exception", str(e))

    @staticmethod
    def log_classes(classes: List[str]) -> None:
        mlflow.set_tag("training.entity.classes", str(classes))

    @staticmethod
    def _get_experiment_id(experiment_name: str) -> str:
        experiment = mlflow.get_experiment_by_name(experiment_name)
        return mlflow.create_experiment(name=experiment_name) if experiment is None else experiment.experiment_id

    @staticmethod
    def _get_experiment_name(model_name: str, training_type: str) -> str:
        return f"{model_name} {training_type}".replace(" ", "_")
