"""Seed the provisioning catalog with initial providers and services.

Run once: python -m app.seed_catalog
"""

import logging
import sys
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import ProvisioningProvider, ProvisioningService

logger = logging.getLogger(__name__)


PROVIDERS = [
    {
        "slug": "cloudflare",
        "name": "Cloudflare",
        "description": "Global cloud platform — CDN, DNS, Workers, R2 storage, Registrar",
        "website": "https://cloudflare.com",
    },
    {
        "slug": "vultr",
        "name": "Vultr",
        "description": "High-performance cloud compute — VPS, bare metal, block storage",
        "website": "https://vultr.com",
    },
    {
        "slug": "github",
        "name": "GitHub",
        "description": "Code hosting, Actions CI/CD, Pages static hosting",
        "website": "https://github.com",
    },
    {
        "slug": "huggingface",
        "name": "Hugging Face",
        "description": "ML model hosting, inference endpoints, datasets",
        "website": "https://huggingface.co",
    },
    {
        "slug": "agentnet",
        "name": "AgentNet",
        "description": "AgentNet-native services — agent hosting, task execution, knowledge graph",
        "website": "https://agentnet.io.vn",
    },
]

SERVICES = {
    "cloudflare": [
        ("domain", "Domain Registration", "Register a new domain via Cloudflare Registrar", "domain",
         "starter", 500, 10.0, ["us", "eu", "ap"], ["domain_name"], {"nameservers": "cloudflare", "token": "api_token"}),
        ("r2-bucket", "R2 Object Storage", "S3-compatible object storage bucket", "storage",
         "free", 0, 0, ["us", "eu", "ap"], ["bucket_name"], {"bucket_url": "...", "endpoint": "..."}),
        ("worker", "Cloudflare Worker", "Serverless compute at the edge", "hosting",
         "free", 0, 0, ["global"], ["worker_name", "script_bundle"], {"worker_url": "...", "token": "api_token"}),
        ("pages", "Cloudflare Pages", "Static site hosting with git integration", "hosting",
         "free", 0, 0, ["global"], ["project_name", "git_repo"], {"pages_url": "...", "token": "api_token"}),
    ],
    "vultr": [
        ("vps", "Cloud Compute VPS", "Virtual private server — 1 vCPU, 1GB RAM", "hosting",
         "starter", 300, 6.0, ["us", "eu", "ap", "sg"], ["region", "os_image"], {"ip": "...", "ssh_key": "..."}),
        ("block-storage", "Block Storage", "SSD-backed persistent block storage", "storage",
         "starter", 100, 1.0, ["us", "eu", "ap"], ["size_gb", "region"], {"mount_path": "...", "volume_id": "..."}),
    ],
    "github": [
        ("repo", "GitHub Repository", "Create a new git repository", "db",
         "free", 0, 0, ["global"], ["repo_name", "visibility"], {"clone_url": "...", "token": "github_pat"}),
        ("pages", "GitHub Pages", "Static site hosting from repo", "hosting",
         "free", 0, 0, ["global"], ["repo_name", "branch"], {"pages_url": "...", "cname": "..."}),
    ],
    "huggingface": [
        ("inference-endpoint", "Inference Endpoint", "Deploy ML model as API endpoint", "ai",
         "starter", 500, 0.06, ["us", "eu"], ["model_id", "instance_type"], {"endpoint_url": "...", "token": "hf_token"}),
        ("space", "Hugging Face Space", "Host ML demo app (Gradio/Streamlit)", "hosting",
         "free", 0, 0, ["us", "eu"], ["space_name", "sdk"], {"space_url": "...", "token": "hf_token"}),
    ],
    "agentnet": [
        ("agent-host", "Agent Hosting", "Deploy an AI agent to AgentNet registry", "ai",
         "free", 0, 0, ["global"], ["agent_name", "capabilities", "endpoint"], {"agent_id": "...", "api_key": "..."}),
        ("knowledge-graph", "Knowledge Graph", "Persistent knowledge graph for agents", "db",
         "free", 0, 0, ["global"], ["graph_name"], {"graph_id": "...", "query_endpoint": "..."}),
    ],
}


def seed():
    db = SessionLocal()
    try:
        # Check if already seeded
        if db.query(ProvisioningProvider).count() > 0:
            logger.info("Catalog already seeded — skipping")
            return

        # Create providers
        provider_objs = {}
        for p in PROVIDERS:
            obj = ProvisioningProvider(**p)
            db.add(obj)
            db.flush()
            provider_objs[p["slug"]] = obj

        # Create services
        for slug, svcs in SERVICES.items():
            provider = provider_objs[slug]
            for (svc_slug, svc_name, desc, cat, tier, creds, usdc, regions, req_params, out_params) in svcs:
                obj = ProvisioningService(
                    provider_id=provider.id,
                    service_name=svc_name,
                    description=desc,
                    category=cat,
                    tier=tier,
                    pricing_credits=creds,
                    pricing_usdc=usdc,
                    regions=regions,
                    required_params=req_params,
                    output_params=out_params,
                )
                db.add(obj)

        db.commit()
        logger.info(
            "Catalog seeded",
            extra={
                "providers": len(PROVIDERS),
                "services": sum(len(v) for v in SERVICES.values()),
            },
        )

    except Exception as e:
        db.rollback()
        logger.exception("Catalog seed failed: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
