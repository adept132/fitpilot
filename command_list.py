import os
import re

commands = set()

for filename in os.listdir('handlers'):
    if filename.endswith('.py'):
        filepath = os.path.join('handlers', filename)

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Ищем Command("...")
        matches = re.findall(r'Command\("([^"]+)"\)', content)
        for cmd in matches:
            commands.add(f"/{cmd}")

print("=" * 50)
print("📋 ВСЕ КОМАНДЫ В ПРОЕКТЕ:")
print("=" * 50)

for cmd in sorted(commands):
    print(cmd)

print("=" * 50)
print(f"Всего: {len(commands)} команд")