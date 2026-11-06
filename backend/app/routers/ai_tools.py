"""AI that does the work, checked by PayRecover: connect any store's data format, and write verified code fixes."""

from fastapi import APIRouter, HTTPException, Request

from app import ai, connector, fixer
from app.schemas import ConnectorProposal, ConnectorRequest, ConnectorStatus, FixProposal, FixRequest, StoreMapping
from app.security import request_owner

router = APIRouter()


@router.get("/api/connector", response_model=ConnectorStatus, tags=["PayRecover"])
def connector_status(http: Request) -> ConnectorStatus:
    """Whether the caller has connected their own store, and how PayRecover reads it."""
    mapping = connector.saved(request_owner(http))
    return ConnectorStatus(connected=mapping is not None, mapping=mapping, example=connector.example())


@router.post("/api/connector/propose", response_model=ConnectorProposal, tags=["PayRecover"])
async def propose_store_mapping(request: ConnectorRequest) -> ConnectorProposal:
    """Read a sample of the store's orders and let the AI propose how to read them, then check the proposal against
    every order and against PayPal. Send a mapping to check your own edit instead. Nothing is saved."""
    try:
        return await connector.propose(request)
    except connector.ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/api/connector", response_model=ConnectorProposal, tags=["PayRecover"])
async def connect_store(mapping: StoreMapping, http: Request) -> ConnectorProposal:
    """Save a mapping, after checking it again. Only a mapping whose checks pass is saved."""
    try:
        mapping = mapping.model_copy(update={"base_url": connector._clean_base(mapping.base_url)})
        checks, preview = await connector.verify(mapping)
    except connector.ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not connector.ready(checks):
        failed = "; ".join(check.message for check in checks if check.status == "failed")
        raise HTTPException(status_code=422, detail=f"Not saved: {failed or 'no order could be read'}.")
    connector.save(mapping, request_owner(http))
    return ConnectorProposal(mapping=mapping, checks=checks, preview=preview, ready=True, source="Saved")


@router.delete("/api/connector", tags=["PayRecover"])
def disconnect_store(http: Request) -> dict[str, bool]:
    """Forget the caller's connected store (nothing in the store changes)."""
    return {"removed": connector.forget(request_owner(http))}


@router.post("/api/fixes", response_model=FixProposal, tags=["PayRecover"])
async def write_fix(request: FixRequest) -> FixProposal:
    """The AI writes the code fix for one finding; PayRecover checks the edits, and for a demo store runs the fixed
    copy to show the hole is closed. No store's files are changed."""
    try:
        return await fixer.propose(request)
    except fixer.FixRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ai.AIConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ai.AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

