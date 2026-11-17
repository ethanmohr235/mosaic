"""Real engineering problems, end to end: a worksheet applied, the garden
solved as a document reads it, and each answer read back through `\\val`
-- the number in the unit asked for, and the unit it is shown in when
none is asked for.

Every expected value is worked out here in plain Python with explicit SI
conversions, never by the app, so a unit the app mishandles cannot agree
with itself. These cases came from a real-world sweep for units and
solver behaviour that was wrong without saying so; each one is a
behaviour a person doing homework would rely on."""
import math

import pytest

from core import modoc, worksheet
from core.modoc_bind import resolve
from core.motes import create_mote
from core.simulation import run_simulation

IN, FT, PSI, LBF = 0.0254, 0.3048, 6894.757293168361, 4.4482216152605


def shown(conn, sheet: str, *refs: str) -> list[str]:
    """Each `\\val{ref}` (or `\\val{ref}{unit, 6}`) as a document shows it,
    after `solve` has written the answers."""
    applied = worksheet.apply(conn, sheet)
    assert not applied.problems, applied.problems
    # Solved and written first, as a person runs `solve`: a stored result
    # must read the same as a live one.
    run_simulation(conn)
    source = " ".join(r"\val{%s}" % ref for ref in refs)
    note = create_mote(conn, {"name": "Check", modoc.MODOC_KEY: source})
    resolution = resolve(conn, note, modoc.parse(source))
    return [value.text for value in resolution.values.values()]


def number(text: str) -> float:
    return float(text.split()[0])


def unit_of(text: str) -> str:
    return text.partition(" ")[2]


# ---- heat exchanger: temperature differences in degF ----

HX = """hx.Thi = 300 degF
hx.Tho = 200 degF
hx.Tci = 70 degF
hx.Tco = 150 degF
hx.U = 50 Btu/(hr*ft^2*degR)
hx.Q = 500000 Btu/hr
hx.dT1 = hx.Thi - hx.Tco
hx.dT2 = hx.Tho - hx.Tci
hx.LMTD = (hx.dT1 - hx.dT2) / ln(hx.dT1 / hx.dT2)
hx.A = hx.Q / (hx.U * hx.LMTD)"""


def test_heat_exchanger_area_from_an_lmtd(conn):
    lmtd = (150 - 130) / math.log(150 / 130)
    area, lmtd_shown = shown(conn, HX, "hx.A}{ft^2, 6", "hx.LMTD}{degR, 6")
    assert number(area) == pytest.approx(500000 / (50 * lmtd), rel=1e-5)
    assert unit_of(area) == "ft²"
    assert number(lmtd_shown) == pytest.approx(lmtd, rel=1e-5)


def test_an_area_from_solved_temperatures_is_an_area(conn):
    (area,) = shown(conn, HX, "hx.A")
    assert "K" not in unit_of(area) and "m²" in unit_of(area)


# ---- cantilever, US customary ----

BEAM = """b.P = 500 lbf
b.L = 6 ft
b.E = 29e6 psi
b.I = 12 in^4
b.c = 2 in
b.d = b.P * b.L^3 / (3 * b.E * b.I)
b.M = b.P * b.L
b.sigma = b.M * b.c / b.I"""


def test_cantilever_deflection_moment_and_stress(conn):
    deflection, moment, stress = shown(conn, BEAM, "b.d}{in, 6", "b.M}{ft*lbf, 6", "b.sigma}{psi, 6")
    assert number(deflection) == pytest.approx(500 * 72**3 / (3 * 29e6 * 12), rel=1e-5)
    assert number(moment) == pytest.approx(3000, rel=1e-6)
    assert number(stress) == pytest.approx(500 * 72 * 2 / 12, rel=1e-5)
    assert unit_of(stress) == "psi"


def test_a_stress_from_a_solved_moment_reads_as_a_pressure(conn):
    (stress,) = shown(conn, BEAM, "b.sigma")
    assert unit_of(stress) in ("Pa", "psi")


# ---- integral of a cosine power shape ----

def test_an_average_over_a_height_is_a_pure_number(conn):
    sheet = """Pz(z [cm]) = cos(pi*z/k.d)
k.d = 109 cm
k.h = 77.5 cm
k.I = integrate(Pz, -k.h/2, k.h/2)
k.avg = k.I / k.h"""
    integral, average = shown(conn, sheet, "k.I}{cm, 6", "k.avg")
    expected = 2 * 109 / math.pi * math.sin(math.pi * 77.5 / (2 * 109))
    assert number(integral) == pytest.approx(expected, rel=1e-5)
    assert number(average) == pytest.approx(expected / 77.5, rel=1e-5)
    assert unit_of(average) == ""


# ---- a count divides a power ----

def test_a_plain_count_divides_a_power_and_keeps_it_a_power(conn):
    sheet = """c.Pt = 1893 MW
c.N = 193 * 204
c.Pav = c.Pt / c.N"""
    (per_rod,) = shown(conn, sheet, "c.Pav}{kW, 6")
    assert number(per_rod) == pytest.approx(1893e3 / (193 * 204), rel=1e-5)
    (default,) = shown(conn, sheet, "c.Pav")
    assert unit_of(default) in ("W", "MW")


# ---- already right: kept right ----

def test_decay_activity_and_mass_of_cs137(conn):
    sheet = """cs.T = 30.08 yr
cs.A0 = 1 Ci
cs.t = 10 yr
cs.M = 136.907 g/mol
cs.NA = 6.02214076e23 1/mol
cs.lam = ln(2) / cs.T
cs.A = cs.A0 * exp(-cs.lam * cs.t)
cs.N = cs.A0 / cs.lam
cs.m = cs.N * cs.M / cs.NA"""
    activity, mass = shown(conn, sheet, "cs.A}{Ci, 6", "cs.m}{g, 6")
    lam = math.log(2) / (30.08 * 365.25 * 86400)
    assert number(activity) == pytest.approx(math.exp(-math.log(2) * 10 / 30.08), rel=1e-5)
    assert number(mass) == pytest.approx(3.7e10 / lam * 136.907 / 6.02214076e23, rel=1e-4)


def test_mesh_currents_in_kohm_and_volts(conn):
    sheet = """ck.V1 = 12 V
ck.V2 = 5 V
ck.R1 = 1 kohm
ck.R2 = 2 kohm
ck.R3 = 3 kohm
ck.I1 [mA] = ?
ck.I2 [mA] = ?
ck.V1 == ck.I1 * (ck.R1 + ck.R2) - ck.I2 * ck.R2
0 == -ck.I1 * ck.R2 + ck.I2 * (ck.R2 + ck.R3) + ck.V2"""
    i1, i2 = shown(conn, sheet, "ck.I1}{mA, 6", "ck.I2}{mA, 6")
    # 3000 I1 - 2000 I2 = 12, -2000 I1 + 5000 I2 = -5
    det = 3000 * 5000 - 2000 * 2000
    assert number(i1) == pytest.approx((12 * 5000 - 2000 * 5) / det * 1e3, rel=1e-6)
    assert number(i2) == pytest.approx((3000 * -5 + 2000 * 12) / det * 1e3, rel=1e-6)


def test_carnot_efficiency_from_fahrenheit_reads_absolute_temperatures(conn):
    sheet = """cy.Th = 1000 degF
cy.Tc = 100 degF
cy.eta = 1 - cy.Tc / cy.Th"""
    (eta,) = shown(conn, sheet, "cy.eta}{6")
    hot, cold = (1000 - 32) * 5 / 9 + 273.15, (100 - 32) * 5 / 9 + 273.15
    assert number(eta) == pytest.approx(1 - cold / hot, rel=1e-6)


def test_force_from_pound_mass_reads_back_in_pound_force(conn):
    (force,) = shown(conn, "f.m = 10 lb\nf.a = 32.174 ft/s^2\nf.F = f.m * f.a", "f.F}{lbf, 6")
    assert number(force) == pytest.approx(10, rel=1e-4)


def test_uncertainty_of_v_squared_over_r(conn):
    (power,) = shown(conn, "u.V = 120 ± 2 V\nu.R = 50 ± 1 ohm\nu.P [W] = u.V^2 / u.R", "u.P}{W, 3")
    assert power == "288 ± 11 W"


def test_a_sweep_of_deflection_against_length(conn):
    from core import plotcmd, plotview
    worksheet.apply(conn, BEAM.replace("b.d =", "b.d [in] ="))
    resolved = plotcmd.resolve(conn, plotcmd.parse_text("plot b.d vs b.L 3:6 --steps 3").line)
    points = plotview.figure_from_spec(conn, resolved.spec, max_points=100).series[0].points
    assert [p[0] for p in points] == [3, 4, 5, 6]
    for length_ft, deflection_in in points:
        inches = length_ft * 12
        assert deflection_in == pytest.approx(500 * inches**3 / (3 * 29e6 * 12), rel=1e-5)


# ---- bare numbers mean the companion's unit ----

@pytest.mark.parametrize("sheet, ref, expected", [
    ("g.L = 10 ft\ng.x [ft] = g.L + 2", "g.x}{ft, 6", 12.0),
    ("g.L = 10 ft\ng.x [ft] = 2 + g.L", "g.x}{ft, 6", 12.0),
    ("g.L = 10 ft\ng.x [ft] = g.L - 2", "g.x}{ft, 6", 8.0),
    ("w.Ti = 543 degF\nw.To [degF] = w.Ti + 10", "w.To}{degF, 6", 553.0),
    ("r.R [kohm] = ?\nr.R == 100", "r.R}{kohm, 6", 100.0),
    ("t.T [degF] = ?\nt.T == 100", "t.T}{degF, 6", 100.0),
    ("g.L = 10 ft\ng.x [ft] = g.L / 2", "g.x}{ft, 6", 5.0),
    ("e.eff = 22.5 %\ne.loss [%] = 1 - e.eff", "e.loss}{%, 6", 77.5),
])
def test_a_bare_number_reads_in_its_companions_unit(conn, sheet, ref, expected):
    (value,) = shown(conn, sheet, ref)
    assert number(value) == pytest.approx(expected, rel=1e-9)


def test_a_bare_number_in_a_function_body_warns_when_it_reads_as_si(conn):
    worksheet.apply(conn, """Pr(r [cm]) = 1 - (r/51)^2
Pz(z [cm]) = cos(pi*z/109)
Ok(r [cm]) = 1 - (r/m.a)^2
m.a = 51 cm
m.r = 25.5 cm
m.v = Pr(m.r)
m.w = Pz(m.r)
m.u = Ok(m.r)""")
    warnings = run_simulation(conn).dimension_warnings
    assert any("1 is subtracted from (r / 51)^2" in w for w in warnings)
    assert any("cos() takes an angle" in w and "109" in w for w in warnings)
    assert not any("m.u" in w for w in warnings)


# ---- temperatures and their differences ----

def test_differences_read_as_degrees_and_temperatures_as_temperatures(conn):
    sheet = HX.replace("degR", "degF") + "\nhx.Tmid = hx.Tci + hx.dT1 / 2"
    d1, lmtd, mid, area = shown(conn, sheet, "hx.dT1", "hx.LMTD", "hx.Tmid", "hx.A}{ft^2, 6")
    assert (number(d1), unit_of(d1)) == (150, "degR")
    assert unit_of(lmtd) == "degR" and number(lmtd) == pytest.approx(139.762, rel=1e-5)
    assert (number(mid), unit_of(mid)) == (145, "degF")
    assert number(area) == pytest.approx(500000 / (50 * 20 / math.log(150 / 130)), rel=1e-5)


def test_celsius_differences_read_in_kelvin(conn):
    sheet = "c.T1 = 20 degC\nc.T2 = 80 degC\nc.d = c.T2 - c.T1\nc.m = c.T1 + c.d / 2"
    difference, middle = shown(conn, sheet, "c.d", "c.m")
    assert difference == "60 K" and middle == "50 degC"


def test_a_difference_declared_in_degf_is_refused(conn):
    applied = worksheet.apply(conn, "hx.Thi = 300 degF\nhx.Tco = 150 degF\nhx.dT1 [degF] = hx.Thi - hx.Tco")
    assert applied.problems and "declare it [degR]" in applied.problems[0]


def test_a_specific_heat_per_fahrenheit_degree(conn):
    # Lamarsh Ex. 8.7: 1.3 Btu/lb-°F water at 3148 lb/hr, 1.07e6 Btu/hr into it.
    sheet = """w.cp = 1.3 Btu/(lb*degF)
w.m = 3148 lb/hr
w.q = 1.0718e6 Btu/hr
w.Ti = 543 degF
w.To = w.Ti + w.q / (w.m * w.cp)"""
    (outlet,) = shown(conn, sheet, "w.To")
    assert unit_of(outlet) == "degF"
    assert number(outlet) == pytest.approx(543 + 1.0718e6 / (3148 * 1.3), rel=1e-5)


def test_a_temperature_less_a_rise_solved_in_kelvin_is_a_temperature(conn):
    sheet = """p.Tc = 1220 degF
p.q = 10 W/cm
p.k = 27 W/(m*K)
p.dT = p.q / (4 * pi * p.k)
p.Tfc = p.Tc - p.dT"""
    rise, interface = shown(conn, sheet, "p.dT", "p.Tfc")
    kelvin = 1000 / (4 * math.pi * 27)
    assert unit_of(rise) == "K" and number(rise) == pytest.approx(kelvin, rel=1e-5)
    assert unit_of(interface) == "degF"
    assert number(interface) == pytest.approx(1220 - kelvin * 1.8, rel=1e-5)


# ---- numbers with units inside equations ----

def test_units_written_in_an_equation(conn):
    sheet = """s.x [m] = ?
s.y [m] = ?
s.y == s.x + 1 m
s.x + s.y == 7 m
w.Ti = 543 degF
w.To = w.Ti + 10 degF
w.cp = 1.3 Btu/(lb*degF)
w.q = w.cp * 1 lb * 10 degF
w.T2 [degF] = ?
w.T2 == 100 degF"""
    x, y, outlet, heat, t2 = shown(conn, sheet, "s.x}{m, 6", "s.y}{m, 6", "w.To}{degF, 6",
                                   "w.q}{Btu, 6", "w.T2}{degF, 6")
    assert [number(v) for v in (x, y, outlet, heat, t2)] == pytest.approx([3, 4, 553, 13, 100])
    assert not run_simulation(conn).dimension_warnings


def test_a_book_answer_as_a_check_with_its_unit(conn):
    from core import calc
    worksheet.apply(conn, """c.a = 3 m
c.b = 4 m
c.h = sqrt(c.a^2 + c.b^2)
c.h ?= 16.4042 ft
c.h <= 6 m""")
    run_simulation(conn)
    rows = calc.checks(conn)
    assert rows and all(row.passed for row in rows), rows


# ---- a given that should have a unit, and doesn't ----

@pytest.mark.parametrize("sheet, given, dimension", [
    # The p29 slip: cp typed bare in the coolant heat balance.
    ("""p.qmax = 4.82e8 W/m^3
p.Vf = 3.27e-4 m^3
p.vQ = 3148 lb/hr
p.cp = 1.3
p.Ti = 543 degF
p.Tb = p.Ti + p.qmax * p.Vf / (pi * p.vQ * p.cp) * 2""", "p.cp", "m²·s⁻²·K⁻¹"),
    ("q.T = 300 degF\nq.dT = 10\nq.T2 = q.T + q.dT", "q.dT", "K"),
    ("v.A = 4\nv.P = 1000 W\nv.q = 250 W/m^2\nv.q == v.P / v.A", "v.A", "m²"),
])
def test_a_given_missing_its_unit_is_named_with_the_dimension_it_needs(conn, sheet, given, dimension):
    worksheet.apply(conn, sheet)
    warnings = run_simulation(conn).dimension_warnings
    assert any(f"{given} has no unit, but this equation needs it in {dimension}" in w
               for w in warnings), warnings


def test_pure_numbers_are_not_asked_for_a_unit(conn):
    worksheet.apply(conn, """r.Pt = 1893 MW
r.N = 39372
r.m2a = 2.32
r.Efrac = 0.9
r.Pc = r.Pt / r.N * r.m2a * r.Efrac""")
    assert not run_simulation(conn).dimension_warnings
