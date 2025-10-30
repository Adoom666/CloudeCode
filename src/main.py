"""Main FastAPI application for Cloude Code Controller."""

import structlog
import asyncio
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from src.config import settings
from src.core.session_manager import SessionManager
from src.core.log_monitor import LogMonitor
from src.core.hybrid_tunnel_manager import HybridTunnelManager
from src.core.auto_tunnel import AutoTunnelOrchestrator
from src.api.routes import router as api_router
from src.api.websocket import router as ws_router
from src.api.auth import router as auth_router

# Configure structlog
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger()


async def verify_public_access():
    """Verify that the public URL is accessible."""
    public_url = f"https://{settings.cloudflare_domain}/health"

    logger.info("verifying_public_access", url=public_url)

    # Wait a bit for DNS to propagate
    await asyncio.sleep(5)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(public_url)

                if response.status_code == 200:
                    logger.info(
                        "public_access_verified",
                        url=f"https://{settings.cloudflare_domain}",
                        status_code=response.status_code
                    )
                    print("\n" + "="*80)
                    print(f"✅ PUBLIC ACCESS VERIFIED!")
                    print(f"🌐 Your API is accessible at: https://{settings.cloudflare_domain}")
                    print(f"📊 Health endpoint: {public_url}")
                    print("="*80 + "\n")
                    return True
                else:
                    logger.warning(
                        "public_access_check_failed",
                        attempt=attempt + 1,
                        status_code=response.status_code
                    )
        except Exception as e:
            logger.warning(
                "public_access_check_error",
                attempt=attempt + 1,
                error=str(e)
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(5)

    logger.error("public_access_verification_failed_after_retries")
    print("\n" + "="*80)
    print(f"⚠️  PUBLIC ACCESS VERIFICATION FAILED")
    print(f"🔗 Expected URL: https://{settings.cloudflare_domain}")
    print(f"💡 The tunnel may still be initializing. Try accessing it in a few moments.")
    print("="*80 + "\n")
    return False


# Global instances (will be initialized in lifespan)
session_manager: SessionManager = None
log_monitor: LogMonitor = None
tunnel_manager: HybridTunnelManager = None
auto_tunnel: AutoTunnelOrchestrator = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global session_manager, log_monitor, tunnel_manager, auto_tunnel

    logger.info("application_starting", version="1.0.0")

    # Initialize core components
    session_manager = SessionManager()
    log_monitor = LogMonitor(session_manager)
    tunnel_manager = HybridTunnelManager(session_manager)

    # Initialize tunnel manager
    await tunnel_manager.initialize()

    auto_tunnel = AutoTunnelOrchestrator(log_monitor, tunnel_manager)

    # Initialize auto-tunnel orchestrator
    auto_tunnel.initialize()

    # Start log monitoring
    await log_monitor.start_monitoring()

    # Make components available to app state
    app.state.session_manager = session_manager
    app.state.log_monitor = log_monitor
    app.state.tunnel_manager = tunnel_manager
    app.state.auto_tunnel = auto_tunnel

    logger.info("application_ready")

    # Verify public URL accessibility if using named tunnels
    if tunnel_manager._is_named:
        await verify_public_access()

    yield

    # Cleanup on shutdown
    logger.info("application_shutting_down")

    await log_monitor.stop_monitoring()
    await auto_tunnel.cleanup()
    await tunnel_manager.shutdown()

    logger.info("application_shutdown_complete")


# Create FastAPI app
app = FastAPI(
    title="☁️ Cloud Code",
    description="Remote control and monitoring for Claude Code sessions",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router, prefix="/api/v1")  # Auth routes (no auth required)
app.include_router(api_router, prefix="/api/v1")   # API routes (auth required)
app.include_router(ws_router)                       # WebSocket routes

# Mount static files
client_dir = Path(__file__).parent.parent / "client"
app.mount("/static", StaticFiles(directory=str(client_dir)), name="static")


@app.get("/")
async def root():
    """Serve the web interface."""
    index_path = client_dir / "index.html"
    return FileResponse(index_path)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "session_active": session_manager.has_active_session() if session_manager else False,
        "monitoring": log_monitor.is_monitoring if log_monitor else False
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level="info"
    )
