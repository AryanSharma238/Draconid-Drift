"""Time a few IRSA queries and say which one works. Run: python scripts/diagnose_irsa.py"""
import time
import pyvo

svc = pyvo.dal.TAPService("https://irsa.ipac.caltech.edu/TAP")
ra, dec = 269.448503, 4.739420   # Barnard's Star
tests = {
 "count planes (tiny query)": "SELECT COUNT(*) AS n FROM spherex.plane",
 "tutorial query, D2 only (sync)": f"""SELECT a.uri, p.time_bounds_lower FROM spherex.artifact a
   JOIN spherex.plane p ON a.planeid = p.planeid
   WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly) AND p.energy_bandpassname = 'SPHEREx-D2'""",
}
for name, q in tests.items():
    t = time.time()
    try:
        r = svc.search(q)
        print(f"OK   {name}: {len(r)} rows in {time.time()-t:.1f}s")
    except Exception as e:
        print(f"FAIL {name} after {time.time()-t:.1f}s: {str(e)[:200]}")
t = time.time()
try:
    j = svc.run_async(tests["tutorial query, D2 only (sync)"])
    print(f"OK   same query async: {len(j)} rows in {time.time()-t:.1f}s")
except Exception as e:
    print(f"FAIL async after {time.time()-t:.1f}s: {str(e)[:200]}")
