from setuptools import find_packages, setup


setup(
    packages=["pppp", *[f"pppp.{name}" for name in find_packages("src")]],
    package_dir={"pppp": "src"},
)
