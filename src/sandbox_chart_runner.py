"""Application-owned entrypoint, executed ONLY inside the Docker sandbox."""
import importlib.util
import runpy
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
from matplotlib.figure import Figure

spec = importlib.util.spec_from_file_location('chart_export', '/script/chart_export.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
exporter.configure_chart_fonts()
original = Figure.savefig


def savefig(figure, fname, *args, **kwargs):
    if isinstance(fname, (str, Path)) and Path(fname).suffix.lower() in {'.png', '.svg'}:
        path = Path(fname)
        exporter.apply_report_theme(figure)
        original(figure, path.with_suffix('.png'), *args,
                 **{**kwargs, 'format': 'png'})
        exporter.export_figure(figure, path.with_suffix('.svg'), original, **kwargs)
    else:
        original(figure, fname, *args, **kwargs)


Figure.savefig = savefig
runpy.run_path('/script/main.py', run_name='__main__')
