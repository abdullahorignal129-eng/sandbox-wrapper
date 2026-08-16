# setup.py
from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="sandbox-wrapper",
    version="0.1.0",
    author="something",
    author_email="something@example.com",
    description="A secure multi-version Python execution wrapper using Windows Sandbox",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/abdullahorignal129-eng/sandbox-wrapper",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Microsoft :: Windows",
    ],
    python_requires=">=3.8",
    install_requires=[],  # No external dependencies
)
