"""core/plotexport.py: a plot as SVG / pgfplots / PDF, with a propagated
± band and measured points over the curve."""
import xml.etree.ElementTree as ET

import pytest

from core import plotcmd, plotexport, tectonic
from core.motes import create_mote, update_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.species import set_species

LINE = "plot C1.current vs C1.voltage 0:300 --steps 10"


@pytest.fixture
def divider(conn):
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = create_mote(conn, {
        "name": "C1", "voltage": 10, "r_a": 100, "r_b": 200,
        "units": {"voltage": "V", "current": "A"},
    })
    set_species(conn, circuit, species)
    return circuit


def test_build_a_plain_sweep_has_no_band_or_overlay(conn, divider):
    pub = plotexport.build(conn, LINE)
    assert pub.sigma == {} and pub.overlay is None
    assert len(pub.figure["series"][0]["points"]) == 11


def test_a_given_with_uncertainty_makes_a_band(conn, divider):
    update_mote(conn, divider, {"uncertainty": {"r_a": 10}})
    pub = plotexport.build(conn, LINE)
    (sigma,) = pub.sigma.values()
    points = pub.figure["series"][0]["points"]
    # I = V / (ra + rb); dI/dra = -V / (ra + rb)^2, so sigma = V * 10 / 300^2
    assert sigma[-1] == pytest.approx(300 * 10 / 300 ** 2, rel=1e-3)
    assert sigma[0] == pytest.approx(0, abs=1e-9)
    assert len(sigma) == len(points)


def test_the_swept_given_adds_no_band_of_its_own(conn, divider):
    update_mote(conn, divider, {"uncertainty": {"voltage": 1}})
    assert plotexport.build(conn, LINE).sigma == {}


def test_measured_points_overlay_with_error_bars(conn, divider):
    create_mote(conn, {"name": "m1", "V": 100, "I": 0.34, "uncertainty": {"I": 0.02}})
    create_mote(conn, {"name": "m2", "V": 200, "I": 0.65})
    pub = plotexport.build(conn, LINE + " --data I vs V")
    assert pub.overlay.points == ((100.0, 0.34, None, 0.02), (200.0, 0.65, None, None))


def test_a_missing_overlay_or_unsupported_mode_is_a_clear_error(conn, divider):
    with pytest.raises(plotexport.PlotExportError, match="no mote carries"):
        plotexport.build(conn, LINE + " --data I vs V")
    with pytest.raises(plotexport.PlotExportError, match="only sweep"):
        plotexport.build(conn, "plot C1.current vs C1.voltage 0:300 by C1.r_a 50:150")
    with pytest.raises(plotexport.PlotExportError, match="needs a plot line"):
        plotexport.build(conn, "plot")


def test_svg_is_well_formed_and_labelled(conn, divider):
    update_mote(conn, divider, {"uncertainty": {"r_a": 10}})
    create_mote(conn, {"name": "m1", "V": 100, "I": 0.34, "uncertainty": {"I": 0.02}})
    text, pub = plotexport.export(conn, LINE + " --data I vs V", "svg")
    root = ET.fromstring(text)
    assert root.tag.endswith("svg")
    assert "C1.voltage (V)" in text and "C1.current" in text
    assert "<polygon" in text and "<circle" in text and "<polyline" in text


def test_log_axis_and_ticks():
    assert plotexport.log_ticks(1, 1000) == [1, 10, 100, 1000]
    assert plotexport.nice_ticks(0, 10)[:3] == [0, 2, 4] or plotexport.nice_ticks(0, 10)[0] == 0
    assert plotexport.tick_text(1e-5) == "1×10⁻⁵" and plotexport.tick_text(2.5) == "2.5"


def test_tex_has_band_data_and_a_standalone_document(conn, divider):
    update_mote(conn, divider, {"uncertainty": {"r_a": 10}})
    create_mote(conn, {"name": "m1", "V": 100, "I": 0.34, "uncertainty": {"I": 0.02}})
    text, pub = plotexport.export(conn, LINE + " --data I vs V", "tex")
    assert text.startswith("\\documentclass[border=6pt]{standalone}")
    assert "fill between" in text and "y error index=3" in text and "only marks" in text
    assert "\\addlegendentry{I vs V}" in text


def test_pdf_compiles_the_standalone_tex(conn, divider, monkeypatch):
    seen = []
    monkeypatch.setattr(tectonic, "compile_pdf", lambda tex, files=None, fetch=None: seen.append(tex) or b"%PDF")
    data, _ = plotexport.export(conn, LINE, "pdf")
    assert data == b"%PDF" and "\\begin{axis}" in seen[0]
    with pytest.raises(plotexport.PlotExportError, match="one of"):
        plotexport.export(conn, LINE, "gif")


def test_data_flag_round_trips_through_the_line():
    line = plotcmd.parse_text("plot a.y vs a.x 0:1 --data I vs V --from bench.csv").line
    assert (line.data, line.source) == (("I", "V"), "bench.csv")
    assert plotcmd.parse_text(line.text()).line == line


# ---- surfaces ----

def _garden(conn, divider):
    update_mote(conn, divider, {"uncertainty": {"r_a": 10}})


def test_cli_plot_export(conn, divider, tmp_path, capsys):
    from cli.main import _handle_plot, build_parser
    out = tmp_path / "fig.svg"
    _handle_plot(conn, build_parser().parse_args(
        ["plot", "export", "svg", "C1.current", "vs", "C1.voltage", "0:300", "--steps", "10", "--out", str(out)]))
    assert out.read_text().startswith("<svg")
    csv_out = tmp_path / "fig.csv"
    _handle_plot(conn, build_parser().parse_args(
        ["plot", "export", "csv", "C1.current", "vs", "C1.voltage", "0:300", "--steps", "10", "--out", str(csv_out)]))
    assert csv_out.read_text().splitlines()[0].startswith("C1.voltage")
    _handle_plot(conn, build_parser().parse_args(["plot", "export", "svg", "C1.current"]))
    assert "Wrote" in capsys.readouterr().out


def test_mcp_export_plot(conn, divider):
    from mcp_server import server
    r = server.export_plot("C1.current vs C1.voltage 0:300 --steps 10", "tex", garden="test")
    assert r["content"].startswith("\\documentclass") and r["problems"] == []
    assert server.export_plot("C1.current vs C1.voltage 0:300 --steps 10", garden="test")["content"].startswith("<svg")


def test_moxie_bridge_writes_the_file(divider, tmp_path):
    from console.api import Api
    api = Api("test")
    try:
        out = tmp_path / "fig.svg"
        r = api.moxie.export_publication("plot C1.current vs C1.voltage 0:300 --steps 10", "svg", str(out))
        assert r["path"] == str(out) and out.read_text().startswith("<svg")
        assert "error" in api.moxie.export_publication("plot", "svg", str(out))
    finally:
        api.shutdown()
