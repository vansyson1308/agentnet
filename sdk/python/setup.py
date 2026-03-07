from setuptools import setup, find_packages

setup(
    name="agentnet",
    version="0.1.0",
    description="AgentNet Python SDK",
    packages=find_packages(),
    install_requires=[
        "httpx>=0.24.0",
        "pydantic>=2.0.0",
    ],
    python_requires=">=3.9",
)
