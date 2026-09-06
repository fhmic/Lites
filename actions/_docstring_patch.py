p = 'actions/code_agent.py'
t = open(p, 'r', encoding='utf-8').read()

# Find the Fallback step
import re
m = re.search(r"  6\. Fallback[^\n]*\n(?:.*\n)*?                   them\.\n", t)
assert m, "Fallback step not found"
print("--- OLD ---")
print(m.group(0))
print("--- END OLD ---")
