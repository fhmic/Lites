import re, ast
p = 'actions/code_agent.py'
with open(p, 'r', encoding='utf-8') as f:
    src = f.read()
try:
    ast.parse(src)
    print("No syntax error")
except SyntaxError as e:
    print("SyntaxError at line", e.lineno, ":", e.msg)
    if e.text:
        print("  text:", repr(e.text))
