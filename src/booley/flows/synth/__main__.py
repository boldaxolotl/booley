"""Run the built-in ASIC synthesis Flow."""

from .flow import AsicSynthesizeFlow

if __name__ == "__main__":
    AsicSynthesizeFlow().cli()
