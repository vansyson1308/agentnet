#!/usr/bin/env python3
import marshal, struct, sys
sys.path.insert(0, '/opt/agentnet')
with open('/opt/agentnet/__pycache__/hermes_agent_base.cpython-311.pyc', 'rb') as f:
    magic = f.read(4)
    flags = struct.unpack('<I', f.read(4))[0]
    if flags & 0x01:
        f.read(4)  # hash
    else:
        f.read(8)
    code = marshal.load(f)

# Print the docstring
print("=== DOCSTRING ===")
print(code.co_consts[0])
print()

# Try to reconstruct by examining all code objects
def dump_code(c, indent=0, seen=None):
    if seen is None:
        seen = set()
    if id(c) in seen:
        return
    seen.add(id(c))
    prefix = "  " * indent
    print(f"{prefix}Code: {c.co_name} (argcount={c.co_argcount}, nlocals={c.co_nlocals}, stack={c.co_stacksize}, flags={c.co_flags})")
    print(f"{prefix}  Varnames: {c.co_varnames}")
    print(f"{prefix}  Names: {c.co_names}")
    # Print relevant constants
    for i, const in enumerate(c.co_consts):
        if isinstance(const, str) and len(const) < 500:
            print(f"{prefix}  const[{i}] str: {repr(const)}")
        elif hasattr(const, 'co_code'):
            print(f"{prefix}  const[{i}] <code {const.co_name}>")
            dump_code(const, indent+2, seen)
    
    # Print bytecode - simplified
    import dis
    instructions = list(dis.get_instructions(c))
    for instr in instructions:
        print(f"{prefix}  {instr.offset:4d} {instr.opname:20s} {instr.argrepr}")

print("=== CODE DUMP ===")
dump_code(code)
