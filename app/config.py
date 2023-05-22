import os
from pydantic import BaseSettings


class Settings(BaseSettings):
    BASE_MODEL_FILE: str = "model.zip"              # the base name of the model file
    BASE_MODEL_FULL_PATH: str = ""                  # the full path to the model file
    DEVICE: str = "cpu"                             # the device literal, either "cpu" or "cuda"
    INCLUDE_SPAN_TEXT: str = "false"                # if "true", include the text of the entity in the NER output
    CONCAT_SIMILAR_ENTITIES: str = "true"           # if "true", merge adjacent entities of the same type into one span
    ENABLE_TRAINING_APIS: str = "false"             # if "true", enable the APIs for model training
    DISABLE_UNSUPERVISED_TRAINING: str = "false"    # if "true", disable the API for unsupervised training
    DISABLE_METACAT_TRAINING: str = "true"          # if "true", disable the API for metacat training
    ENABLE_EVALUATION_APIS: str = "false"           # if "true", enable the APIs for evaluating the model being served
    ENABLE_PREVIEWS_APIS: str = "false"             # if "true", enable the APIs for previewing the NER output
    MLFLOW_TRACKING_URI: str = f'file:{os.path.join(os.path.abspath(os.path.dirname(__file__)), "mlruns")}'     # the mlflow tracking URI
    REDEPLOY_TRAINED_MODEL: str = "false"           # if "true", replace the running model with the newly trained one
    SKIP_SAVE_MODEL: str = "false"                  # if "true", newly trained models won't be saved but training metrics will be collected
    SKIP_SAVE_TRAINING_DATASET: str = "true"        # if "true", the dataset used for training won't be saved
    PROCESS_RATE_LIMIT: str = "180/minute"          # the rate limit on the /process route
    PROCESS_BULK_RATE_LIMIT: str = "90/minute"      # the rate limit on the /process_bulk route
    TYPE_UNIQUE_ID_WHITELIST: str = ""              # the comma-separated TUIs used for filtering and if set to "", all TUIs are whitelisted

    class Config:
        env_file = os.path.join(os.path.dirname(__file__), "envs", ".env")
        env_file_encoding = "utf-8"
