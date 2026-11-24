import sys
import os
import json
import re
import httpx
from openai import OpenAI

def parse_diff(diff_text):
    valid_lines = set()
    formatted_diff = []
    current_file = None
    current_line_new = 0
    
    for line in diff_text.splitlines():
        if line.startswith('Binary files'):
            current_file = None
            continue
        
        if line.startswith('diff --git'):
            match = re.search(r'b/(.+)', line)
            if match:
                current_file = match.group(1)
                formatted_diff.append(f"\n=== ФАЙЛ: {current_file} ===")
            else:
                current_file = None
            continue
            
        if not current_file:
            continue

        if line.startswith('@@'):
            match = re.search(r'\+(\d+)', line)
            if match:
                current_line_new = int(match.group(1))
            formatted_diff.append(line)
            continue

        if line.startswith('+'):
            formatted_diff.append(f"[{current_line_new}] {line}")
            valid_lines.add((current_file, current_line_new))
            current_line_new += 1
        elif line.startswith('-'):
            formatted_diff.append(f"[---] {line}")
        elif line.startswith(' '):
            formatted_diff.append(f"[{current_line_new}] {line}")
            valid_lines.add((current_file, current_line_new))
            current_line_new += 1
        else:
            formatted_diff.append(line)

    return "\n".join(formatted_diff), valid_lines

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"summary": "Ошибка: не передан файл diff", "comments": []}))
        return
    
    diff_path = sys.argv[1]
    try:
        with open(diff_path, 'r', encoding='utf-8') as f:
            raw_diff = f.read()
    except Exception as e:
        print(json.dumps({"summary": f"Ошибка чтения diff: {e}", "comments": []}))
        return

    if not raw_diff.strip():
        print(json.dumps({"summary": "Нет изменений для проверки", "comments": []}))
        return

    formatted_diff, valid_lines = parse_diff(raw_diff)

    diff_lines = [
    l for l in raw_diff.splitlines()
    if (l.startswith('+') and not l.startswith('+++'))
    or (l.startswith('-') and not l.startswith('---'))
]
    file_count = raw_diff.count('diff --git')
    if file_count > 30:
        print(json.dumps({
            "summary": f"PR слишком большой для автоматического ревью ({file_count} файлов). Максимум 30. Ревью пропущено.",
            "comments": []
        }))
        return

    if len(diff_lines) > 1000:
        print(json.dumps({
            "summary": f"PR слишком большой для автоматического ревью ({len(diff_lines)} строк). Ревью пропущено.",
            "comments": []
        }))
        return

    instructions = "Ищи ошибки."
    if os.path.exists('.github/review-instructions.md'):
        with open('.github/review-instructions.md', 'r', encoding='utf-8') as f:
            instructions = f.read()

    if os.path.exists('AGENTS.md'):
        with open('AGENTS.md', 'r', encoding='utf-8') as f:
            instructions += "\n\n=== ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ ПРОЕКТА (AGENTS.md) ===\n"
            instructions += f.read()

    # 3. Формирование промта 
    prompt = f"""
Ты — код-ревьюер. Твоя задача — найти проблемы в диффе согласно инструкциям.
Верни СТРОГО JSON: {{"summary": "общий итог", "comments": [{{"file": "путь", "line": число, "severity": "error|warning", "message": "текст", "reason": "почему это ошибка", "suggestion": "код или null"}}]}}

Инструкции:
{instructions}

Вот diff. Числа в квадратных скобках [12] — это номера строк в новой версии файла. Используй ТОЛЬКО ИХ для поля "line".
{formatted_diff}
"""
    try:


        client = OpenAI(
            api_key=os.environ.get("GEMINI_API_KEY"),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            timeout=httpx.Timeout(60.0, connect=10.0)
        )

    
        response = client.chat.completions.create(
            model="gemini-3.8-flash",
            messages=[
                {"role": "system", "content": "Ты — строгий код-ревьюер, который возвращает только JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        raw = response.choices[0].message.content
        
        # снятие markdown-обертки, если модель её добавила
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        content = json.loads(raw)

        # Защита от невалидной структуры
        if not isinstance(content, dict):
            content = {}
        if not isinstance(content.get('summary'), str):
            content['summary'] = "Ревью выполнено."
        if not isinstance(content.get('comments'), list):
            content['comments'] = []
        
        valid_comments = []
        fallback_messages = []
        
        for c in content.get('comments', []):
            file_path = c.get('file')
            line_num = c.get('line')
            
            if (file_path, line_num) in valid_lines:
                valid_comments.append(c)
            else:
                fallback_messages.append(f"Файл `{file_path}`, строка {line_num}: {c.get('message')}")

        if fallback_messages:
            content['summary'] += "\n\n**Замечания вне измененного кода:**\n" + "\n".join(f"- {msg}" for msg in fallback_messages)
        
        content['comments'] = valid_comments
        print(json.dumps(content))
        
    except Exception as e:
        print(f"Ошибка LLM: {e}", file=sys.stderr)
        print(json.dumps({"summary": f"Ошибка при запросе к нейросети: {e}", "comments": []}))

if __name__ == "__main__":
    main()