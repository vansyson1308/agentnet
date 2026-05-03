"""Minimal setup for `pip install -e sdk/python`."""

from setuptools import find_packages, setup

setup(
    name="agentnet-sdk",
    version="0.2.0",
    description="Python SDK for the AgentNet protocol — agent economy on FastAPI + Postgres + Redis.",
    long_description=(
        "Synchronous client for the AgentNet registry + payment services, plus an "
        "optional WebSocket client (`agentnet[ws]`) for event-driven agents."
    ),
    long_description_content_type="text/plain",
    author="AgentNet",
    license="Apache-2.0",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "httpx>=0.25",
    ],
    extras_require={
        "ws": ["websockets>=12.0"],
        "dev": ["pytest", "pytest-asyncio", "websockets>=12.0"],
    },
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
