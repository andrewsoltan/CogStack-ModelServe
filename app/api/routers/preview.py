import uuid
import json
from io import BytesIO
from typing import Union
from typing_extensions import Annotated

from fastapi import APIRouter, Depends, Body, UploadFile, Request, Response, File, Query
from fastapi.responses import StreamingResponse, JSONResponse
from spacy import displacy
from starlette.status import HTTP_404_NOT_FOUND

import api.globals as cms_globals
from domain import Doc, Tags
from model_services.base import AbstractModelService
from utils import annotations_to_entities

router = APIRouter()


@router.post("/preview",
             tags=[Tags.Rendering.name],
             response_class=StreamingResponse,
             dependencies=[Depends(cms_globals.props.current_active_user)],
             description="Extract the NER entities in HTML for preview")
async def get_rendered_entities_from_text(request: Request,
                                          text: Annotated[str, Body(description="The text to be sent to the model for NER", media_type="text/plain")],
                                          model_service: AbstractModelService = Depends(cms_globals.model_service_dep)) -> StreamingResponse:
    annotations = model_service.annotate(text)
    entities = annotations_to_entities(annotations, model_service.model_name)
    ent_input = Doc(text=text, ents=entities)
    data = displacy.render(ent_input.dict(), style="ent", manual=True)
    response = StreamingResponse(BytesIO(data.encode()), media_type="application/octet-stream")
    response.headers["Content-Disposition"] = f'attachment ; filename="preview_{str(uuid.uuid4())}.html"'
    return response


@router.post("/preview_trainer_export",
             tags=[Tags.Rendering.name],
             response_class=StreamingResponse,
             dependencies=[Depends(cms_globals.props.current_active_user)],
             description="Get existing entities in HTML from a trainer export for preview")
def get_rendered_entities_from_trainer_export(request: Request,
                                              trainer_export: Annotated[UploadFile, File(description="The trainer export file to be uploaded")],
                                              project_id: Annotated[Union[int, None], Query(description="The target project ID, and if not provided, all projects will be included")] = None,
                                              document_id: Annotated[Union[int, None], Query(description="The target document ID, and if not provided, all documents of the target project(s) will be included")] = None) -> Response:
    data = json.load(trainer_export.file)
    htmls = []
    for project in data["projects"]:
        if project_id is not None and project_id != project["id"]:
            continue
        for document in project["documents"]:
            if document_id is not None and document_id != document["id"]:
                continue
            entities = []
            for annotation in document["annotations"]:
                entities.append({
                    "start": annotation["start"],
                    "end": annotation["end"],
                    "label": f"{annotation['cui']} ({'correct' if annotation['correct'] else 'incorrect'}{'; terminated' if annotation['killed'] else ''})",
                    "kb_id": annotation["cui"],
                    "kb_url": "#",
                })
            # Displacy cannot handle annotations out of appearance order so be this
            entities = sorted(entities, key=lambda e: e["start"])
            doc = Doc(text=document["text"], ents=entities, title=f"P{project['id']}/D{document['id']}")
            htmls.append(displacy.render(doc.dict(), style="ent", manual=True))
    if htmls:
        response = StreamingResponse(BytesIO("<br/>".join(htmls).encode()), media_type="application/octet-stream")
        response.headers["Content-Disposition"] = f'attachment ; filename="preview_{str(uuid.uuid4())}.html"'
    else:
        return JSONResponse(content={"message": "Cannot find any matching documents to preview"}, status_code=HTTP_404_NOT_FOUND)
    return response
