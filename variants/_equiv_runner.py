
import importlib.util, io, json, sys
from contextlib import redirect_stdout
spec = importlib.util.spec_from_file_location("bot", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["bot"] = module
spec.loader.exec_module(module)
sys.path.insert(0, "harness")
import real_sim
out = {}
for case in real_sim.load_cases():
    values = []
    for rep in range(int(sys.argv[2])):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = real_sim.run_case(case, seed=7000 + rep * 13, days=None, use_live=True)
        values.append(round(result["Telescoping Theo"], 6))
    out[int(case["testcase_id"])] = values
print("###" + json.dumps(out))
