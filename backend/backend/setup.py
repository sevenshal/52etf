from setuptools import setup, find_packages

setup(
    name="quant",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.6",
    include_package_data=True,
) 
