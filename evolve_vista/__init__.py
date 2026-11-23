"""EvolveVista: follow an InfiniEvolve campaign in the browser, its lineage and best-so-far curve per task, and
open any session as a TraceVista analysis.

    evolve-vista /path/to/campaign            or, from InfiniEvolve:  infinievolve watch <campaign>

The trajectory viewer and the TraceVista skill come from TraceVista (third_party/TraceVista).
"""
from .server import DEFAULT_PORT, Reader, VistaServer, start
from .vista import Analysts

__all__ = ["Analysts", "DEFAULT_PORT", "Reader", "VistaServer", "start"]
