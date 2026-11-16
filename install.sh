#!/usr/bin/env bash
# agent-autolearn installer (multi-harness: OpenCode v1, OpenCode v2, pi)
#
# Usage:
#   bash install.sh                    # run from cloned repo
#   bash install.sh /path/to/repo      # specify repo path
#
# Or one-liner:
#   curl -fsSL https://raw.githubusercontent.com/ericmjl/agent-autolearn/main/install.sh | bash
#
# Installs for every harness detected on the machine:
#   - OpenCode v1/v2: plugins + opencode.json patch
#   - pi:             extension + shared core in ~/.pi/agent/extensions/
# The skills + store (~/.autolearn) are shared by all harnesses.

set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")" && pwd)}"

if [[ ! -f "$REPO_DIR/plugin/autolearn.js" ]]; then
  echo "Error: could not find plugin/autolearn.js in $REPO_DIR"
  echo "Clone the repo first: git clone https://github.com/ericmjl/agent-autolearn.git"
  exit 1
fi

PLUGIN_DIR="$HOME/.config/opencode/plugins"
PI_EXT_DIR="$HOME/.pi/agent/extensions"
SKILLS_DIR="$HOME/.agents/skills"
OPENCODE_JSON="$HOME/.config/opencode/opencode.json"

echo "agent-autolearn installer"
echo "========================"
echo ""

# 1. Copy plugins/extensions. The core uses a .mjs extension so OpenCode v2's
# plugins/ auto-discovery and pi's extensions/ discovery do not try to load
# it as a plugin (both only discover .js/.ts files).
echo "[1/5] Installing plugins/extensions..."
mkdir -p "$PLUGIN_DIR"
cp "$REPO_DIR/plugin/autolearn.js" "$PLUGIN_DIR/"
cp "$REPO_DIR/plugin/autolearn-v2.js" "$PLUGIN_DIR/"
cp "$REPO_DIR/plugin/autolearn-core.mjs" "$PLUGIN_DIR/"
# Remove any stray pre-.mjs core copy from earlier installs (the .js name
# would be picked up by OpenCode v2's plugins/ auto-discovery and fail
# schema validation with a warning).
rm -f "$PLUGIN_DIR/autolearn-core.js"

# pi extension (both files land in the extensions dir; the core's .mjs name
# keeps pi's discovery from loading it as an extension, while the shell's
# relative import still resolves).
mkdir -p "$PI_EXT_DIR"
cp "$REPO_DIR/plugin/autolearn-pi.ts" "$PI_EXT_DIR/"
cp "$REPO_DIR/plugin/autolearn-core.mjs" "$PI_EXT_DIR/"

# 2. Install the single consolidated skill, replacing the three legacy skill
# dirs (autolearn-reviewer, autolearn-curator, self-improving-agent). Only
# removes the legacy dirs this installer owns; never touches agent-created
# skills (e.g. autolearn-audit) or user-installed skills.
echo "[2/5] Installing skills..."
mkdir -p "$SKILLS_DIR"
rm -rf "$SKILLS_DIR/autolearn-reviewer" "$SKILLS_DIR/autolearn-curator" "$SKILLS_DIR/self-improving-agent"
rm -rf "$SKILLS_DIR/autolearn"
cp -r "$REPO_DIR/skills/autolearn" "$SKILLS_DIR/"

# 3. Patch opencode.json
echo "[3/5] Configuring opencode.json..."
mkdir -p "$(dirname "$OPENCODE_JSON")"

python3 -c "
import json, os, sys, re, shutil, subprocess

path = '$OPENCODE_JSON'

# OpenCode v2 validates plugin entries against its object-based API. Do not
# register the legacy v1 function plugin there: it only creates a startup
# warning and is not used by the v2 shell.
opencode_major = None
for executable in ('opencode2', 'opencode'):
    resolved = shutil.which(executable)
    if not resolved:
        continue
    try:
        proc = subprocess.run([resolved, '--version'], capture_output=True,
                              text=True, timeout=5)
        match = re.search(r'\\b(\\d+)\\.', (proc.stdout or '') + ' ' + (proc.stderr or ''))
        if match:
            opencode_major = int(match.group(1))
            break
    except (OSError, subprocess.SubprocessError):
        pass
is_opencode_v2 = opencode_major is not None and opencode_major >= 2
if is_opencode_v2:
    legacy_plugin_file = os.path.join(os.path.dirname(path), 'plugins', 'autolearn.js')
    if os.path.exists(legacy_plugin_file):
        os.remove(legacy_plugin_file)

if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    data = {}

changed = False

# plugin — v1 shell. Keep it only on OpenCode v1; v2 uses the object-based
# plugin API and must not try to load this legacy function export.
plugin_entry = './plugins/autolearn.js'
legacy_plugins = data.get('plugin', [])
legacy_plugins = legacy_plugins if isinstance(legacy_plugins, list) else [legacy_plugins]
if is_opencode_v2:
    filtered = [p for p in legacy_plugins if p != plugin_entry]
    if filtered != legacy_plugins:
        if filtered:
            data['plugin'] = filtered
        else:
            data.pop('plugin', None)
        changed = True
else:
    if plugin_entry not in legacy_plugins:
        legacy_plugins.append(plugin_entry)
        data['plugin'] = legacy_plugins
        changed = True

# plugins — v2 shell (native v2 key; OpenCode v1 ignores unknown top-level
# keys, so both versions can share this file).
if 'plugins' not in data:
    data['plugins'] = []
plugins_v2 = data['plugins'] if isinstance(data['plugins'], list) else [data['plugins']]
plugin_v2_entry = './plugins/autolearn-v2.js'
if plugin_v2_entry not in plugins_v2:
    plugins_v2.append(plugin_v2_entry)
    data['plugins'] = plugins_v2
    changed = True

# instructions — point at the generated context view (Memory Insight);
# strip any superseded memory.md paths (persona + flat-layout).
if 'instructions' not in data:
    data['instructions'] = []
instructions = data['instructions'] if isinstance(data['instructions'], list) else [data['instructions']]
ctx_entry = os.path.expanduser('~/.autolearn/personas/default/memory.context.md')
# Strip superseded memory.md paths (persona + flat layout), matching both
# expanded absolute paths and literal-tilde forms.
superseded = {
    os.path.expanduser('~/.autolearn/personas/default/memory.md'),
    os.path.expanduser('~/.autolearn/memory.md'),
    '~/.autolearn/personas/default/memory.md',
    '~/.autolearn/memory.md',
}
if any(i in superseded for i in instructions):
    instructions = [i for i in instructions if i not in superseded]
    data['instructions'] = instructions
    changed = True
if ctx_entry not in instructions:
    instructions.append(ctx_entry)
    data['instructions'] = instructions
    changed = True

# agent — the reviewer role the plugin spawns via 'run --agent'. The prompt
# points at the consolidated autolearn skill (reviewer reference).
if 'agent' not in data:
    data['agent'] = {}
reviewer_prompt = (
    'Load the autolearn skill and follow references/reviewer.md to review '
    'the attached conversation for learning opportunities. Take immediate '
    'action: record observations, update memory, create or patch skills.'
)
if 'autolearn-reviewer' not in data['agent']:
    data['agent']['autolearn-reviewer'] = {
        'description': 'Reviews past conversations for self-improvement opportunities',
        'hidden': True,
        'steps': 20,
        'prompt': reviewer_prompt,
        'permission': {
            'bash': 'allow',
            'read': 'allow',
            'glob': 'allow',
            'grep': 'allow',
            'write': 'allow',
            'edit': 'deny',
            'webfetch': 'deny',
            'task': 'deny',
            'skill': 'allow',
            'external_directory': 'allow'
        }
    }
    changed = True
elif data['agent']['autolearn-reviewer'].get('prompt') != reviewer_prompt:
    data['agent']['autolearn-reviewer']['prompt'] = reviewer_prompt
    changed = True

# OpenCode v2 uses the plural `agents` key and requires a runnable
# agent mode. Keep the legacy singular `agent` entry above for v1.
if 'agents' not in data or not isinstance(data['agents'], dict):
    data['agents'] = {}
reviewer_v2_prompt = (
    'You are the autolearn reviewer. Read '
    + os.path.expanduser('~/.agents/skills/autolearn/SKILL.md')
    + ' first, then read '
    + os.path.expanduser('~/.agents/skills/autolearn/references/reviewer.md')
    + ' and follow its Action Protocol exactly. The conversation slice to '
    'review is the current prompt. Take action only in the autolearn store; '
    'never modify project source code.'
)
reviewer_v2 = data['agents'].setdefault('autolearn-reviewer', {})
if not isinstance(reviewer_v2, dict):
    reviewer_v2 = {}
    data['agents']['autolearn-reviewer'] = reviewer_v2
for key, value in {
    'description': 'Autolearn reviewer: extracts durable lessons from conversation slices',
    'mode': 'all',
    'prompt': reviewer_v2_prompt,
}.items():
    if reviewer_v2.get(key) != value:
        reviewer_v2[key] = value
        changed = True

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')

if changed:
    print('  Updated ' + path)
else:
    print('  Already configured (' + path + ')')
" 2>&1

# 4. Initialize + bootstrap the registry (migrates legacy memory.md if present)
echo "[4/5] Initializing autolearn store..."
CLI="$SKILLS_DIR/autolearn/scripts/autolearn.py"
if [[ ! -f "$CLI" ]]; then
  CLI="$SKILLS_DIR/autolearn-reviewer/scripts/autolearn.py"
fi
if command -v uv &>/dev/null; then
    uv run "$CLI" init
    uv run "$CLI" retention score   # migrates legacy store -> registry, scores tiers
    uv run "$CLI" memory compose    # generate memory.context.md from the registry
else
    echo "  Warning: uv not found. Run manually: uv run $CLI init"
fi

# 5. Verify
echo "[5/5] Verifying..."
OK=true
if [[ -f "$PLUGIN_DIR/autolearn.js" ]]; then
  :
elif grep -q 'autolearn-v2.js' "$OPENCODE_JSON" 2>/dev/null; then
  echo "  Legacy v1 shell intentionally omitted for OpenCode v2."
else
  echo "  MISSING: $PLUGIN_DIR/autolearn.js"
  OK=false
fi
[[ -f "$PLUGIN_DIR/autolearn-v2.js" ]] || { echo "  MISSING: $PLUGIN_DIR/autolearn-v2.js"; OK=false; }
[[ -f "$PLUGIN_DIR/autolearn-core.mjs" ]] || { echo "  MISSING: $PLUGIN_DIR/autolearn-core.mjs"; OK=false; }
[[ -f "$PI_EXT_DIR/autolearn-pi.ts" ]] || { echo "  MISSING: $PI_EXT_DIR/autolearn-pi.ts"; OK=false; }
[[ -f "$PI_EXT_DIR/autolearn-core.mjs" ]] || { echo "  MISSING: $PI_EXT_DIR/autolearn-core.mjs"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/SKILL.md" ]] || { echo "  MISSING: skills/autolearn"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/scripts/autolearn.py" ]] || { echo "  MISSING: skills/autolearn/scripts/autolearn.py"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/scripts/improve.py" ]] || { echo "  MISSING: skills/autolearn/scripts/improve.py"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/scripts/evolution.py" ]] || { echo "  MISSING: skills/autolearn/scripts/evolution.py"; OK=false; }
[[ -f "$SKILLS_DIR/autolearn/references/evolution.md" ]] || { echo "  MISSING: skills/autolearn/references/evolution.md"; OK=false; }
[[ -f "$HOME/.autolearn/personas/default/memory.context.md" ]] || { echo "  MISSING: ~/.autolearn/personas/default/memory.context.md"; OK=false; }
[[ -f "$HOME/.autolearn/personas/default/memories.jsonl" ]] || { echo "  MISSING: ~/.autolearn/personas/default/memories.jsonl"; OK=false; }

echo ""
if $OK; then
    echo "Done! Autolearn will activate on your next OpenCode or pi session."
else
    echo "Some files are missing — check the errors above."
    exit 1
fi
