"""
API routes for the Claude-config file tree + editor (see
``src/core/config_files.py`` for the actual list/read/write logic and
the allowed-roots / hide-list constants - this module is deliberately
thin: request/response shape, auth, and error translation only).

Mounted under ``/api/v1`` from ``src/main.py`` alongside the other
routers. Every endpoint requires ``Depends(require_auth)`` - this is
config content, not public data.

Security note: request bodies/params are logged only at the field-name
level (never file content) - see the ``logger.info`` calls below and in
``config_files.write_file``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import structlog

from src.api.auth import require_auth
from src.core import config_files, config_files_create

logger = structlog.get_logger()

router = APIRouter(prefix="/config-files", tags=["config-files"])


class ConfigFileWriteRequest(BaseModel):
    """Body for POST /config-files/write.

    Inputs (fields):
      root (str) - "user", "project", or "workdir".
      path (str) - rel_path from a tree listing.
      content (str) - new file contents.
      project_path (str|None) - required when root != "user".
      acknowledge_executable (bool) - must be True to write a file
        config_files.py classifies as executable; the client sets this
        only after ``App.showConfirmModal()`` returned true.
      acknowledge_sensitive (bool) - must be True to write a file
        config_files.py classifies as sensitive (credentials/secret/key-
        shaped); the client sets this only after
        ``App.showConfirmModal()`` returned true.
    """
    root: str
    path: str
    content: str
    project_path: Optional[str] = None
    acknowledge_executable: bool = False
    acknowledge_sensitive: bool = False


class ConfigFileCreateRequest(BaseModel):
    """Body for POST /config-files/create.

    Same fields as ConfigFileWriteRequest, with ``content`` defaulting to
    empty so "make me an empty file" needs no body gymnastics. ``path``
    names a file that must NOT already exist; its parent directory must.
    """
    root: str
    path: str
    content: str = ""
    project_path: Optional[str] = None
    acknowledge_executable: bool = False
    acknowledge_sensitive: bool = False


class ConfigFileCreateResponse(BaseModel):
    ok: bool = True
    created: bool
    rel_path: str
    is_executable: bool
    is_sensitive: bool


class ConfigFileTreeResponse(BaseModel):
    root: str
    tree: list


class ConfigFileReadResponse(BaseModel):
    content: str
    is_executable: bool
    is_sensitive: bool
    read_only: bool
    size: int


class ConfigFileWriteResponse(BaseModel):
    ok: bool = True
    backed_up: bool
    is_executable: bool
    is_sensitive: bool


@router.get("/tree", response_model=ConfigFileTreeResponse, dependencies=[Depends(require_auth)])
async def get_config_file_tree(
    root: str = Query(..., description='"user", "project", or "workdir"'),
    project_path: Optional[str] = Query(None),
):
    """
    Description: list the hide-list-filtered claude-config/project file
      tree for one root ("user"/"project" are also allow-listed;
      "workdir" is not).
    Inputs: root (str, query) - "user", "project", or "workdir";
      project_path (str|None, query) - required for root != "user".
    Output: ConfigFileTreeResponse.
    Raises:
      HTTPException(400) - unknown/unavailable root (translated from
        config_files.ConfigFileError).
      HTTPException(503) - the root exists but could not be read
        (translated from config_files.ConfigFileUnreadableError, which
        is checked FIRST since it subclasses ConfigFileError - a
        permissions problem is "could not evaluate", not "bad request",
        per this project's three-outcome rule; the client renders this
        as a distinct error row rather than folding it into "empty").
    """
    try:
        tree = config_files.list_tree(root, project_path)
    except config_files.ConfigFileUnreadableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except config_files.ConfigFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ConfigFileTreeResponse(root=root, tree=tree)


@router.get("/read", response_model=ConfigFileReadResponse, dependencies=[Depends(require_auth)])
async def read_config_file(
    root: str = Query(...),
    path: str = Query(...),
    project_path: Optional[str] = Query(None),
):
    """
    Description: read one browsable file's contents. Sensitive files
      (credentials/secret/key-shaped) are returned, not refused - the
      response's ``is_sensitive`` flag tells the client to mask the
      content until the user explicitly reveals it.
    Inputs: root (str, query); path (str, query) - rel_path from a tree
      listing; project_path (str|None, query).
    Output: ConfigFileReadResponse.
    Raises: HTTPException(400) - invalid path, hidden file, path
      traversal, oversized file, or binary/non-utf8 content (all
      translated from config_files.ConfigFileError - never a 500 for a
      client mistake).
    """
    try:
        result = config_files.read_file(root, path, project_path)
    except config_files.ConfigFileError as exc:
        logger.warning("config_files_read_rejected", root=root, path=path, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    return ConfigFileReadResponse(**result)


@router.post("/write", response_model=ConfigFileWriteResponse, dependencies=[Depends(require_auth)])
async def write_config_file(body: ConfigFileWriteRequest):
    """
    Description: write one browsable file. Always backs up the previous
      bytes first; validates JSON for .json files before writing;
      refuses executable files (hooks/, scripts/) without
      ``acknowledge_executable=True``; refuses sensitive files
      (credentials/secret/key-shaped) without
      ``acknowledge_sensitive=True``; refuses anything under a
      read-only root (plugins/) or outside the allowed roots entirely.
      Never logs ``body.content``.
    Inputs: body (ConfigFileWriteRequest).
    Output: ConfigFileWriteResponse.
    Raises: HTTPException(400) - any rejection from
      config_files.write_file (bad path, malformed JSON, read-only,
      unacknowledged executable write, unacknowledged sensitive write) -
      translated from config_files.ConfigFileError.
    """
    logger.info(
        "config_files_write_request",
        root=body.root,
        path=body.path,
        acknowledge_executable=body.acknowledge_executable,
        acknowledge_sensitive=body.acknowledge_sensitive,
    )
    try:
        result = config_files.write_file(
            root_id=body.root,
            rel_path=body.path,
            content=body.content,
            project_path=body.project_path,
            acknowledge_executable=body.acknowledge_executable,
            acknowledge_sensitive=body.acknowledge_sensitive,
        )
    except config_files.ConfigFileError as exc:
        logger.warning("config_files_write_rejected", root=body.root, path=body.path, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    return ConfigFileWriteResponse(**result)


@router.post("/create", response_model=ConfigFileCreateResponse, dependencies=[Depends(require_auth)])
async def create_config_file(body: ConfigFileCreateRequest):
    """
    Description: create one NEW file under a browsable root. Refuses to
      overwrite anything that already exists at that path; requires the
      parent directory to exist (this endpoint never creates directories);
      validates JSON for a .json name; and applies the same executable /
      sensitive acknowledgement gates and the same read-only-root refusal
      as /write. Path safety is the SAME guard as every other endpoint
      here - config_files.resolve_safe_path, reached through
      config_files_create.create_file. Never logs ``body.content``.
    Inputs: body (ConfigFileCreateRequest).
    Output: ConfigFileCreateResponse.
    Raises: HTTPException(400) - any rejection from
      config_files_create.create_file (traversal, absolute path, existing
      name, missing parent, read-only root, malformed JSON, missing
      acknowledgement), translated from config_files.ConfigFileError.
    """
    logger.info(
        "config_files_create_request",
        root=body.root,
        path=body.path,
        acknowledge_executable=body.acknowledge_executable,
        acknowledge_sensitive=body.acknowledge_sensitive,
    )
    try:
        result = config_files_create.create_file(
            root_id=body.root,
            rel_path=body.path,
            content=body.content,
            project_path=body.project_path,
            acknowledge_executable=body.acknowledge_executable,
            acknowledge_sensitive=body.acknowledge_sensitive,
        )
    except config_files.ConfigFileError as exc:
        logger.warning("config_files_create_rejected", root=body.root, path=body.path, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    return ConfigFileCreateResponse(**result)
