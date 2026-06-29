import os
import re


def count_file_metrics(file_path):
    """Подсчитывает строки и функции в одном файле."""
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    # Общее количество строк
    total_lines = len(lines)

    # Количество функций (def или async def)
    function_count = sum(1 for line in lines if re.match(r'^\s*(def|async def)\s+', line.strip()))

    return total_lines, function_count


def scan_directory(directory):
    """Рекурсивно сканирует директорию и подсчитывает метрики."""
    total_lines = 0
    total_functions = 0
    file_count = 0
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                file_path = os.path.join(root, file)
                lines, funcs = count_file_metrics(file_path)
                total_lines += lines
                total_functions += funcs
                file_count += 1

    print("-" * 60)
    print(f"Папка: {directory}")
    print(f"Итого файлов (.py): {file_count}")
    print(f"Итого строк: {total_lines}")
    print(f"Итого функций: {total_functions}")
    print(f"Среднее строк на файл: {total_lines / file_count:.2f}" if file_count else 0)
    return [total_lines, total_functions, file_count]

if __name__ == "__main__":
    project_dirs = [
        "C:/Users/Admin/PycharmProjects/FitPilotBot/app",
        "C:/Users/Admin/PycharmProjects/FitPilotBot/bot",
        "C:/Users/Admin/PycharmProjects/FitPilotBot/handlers",
        "C:/Users/Admin/PycharmProjects/FitPilotBot/services"
    ]

    all_lines, all_functions, all_files = 0, 0, 0

    for project_dir in project_dirs:
        if not os.path.isdir(project_dir):
            print(f"Директория '{project_dir}' не найдена.")
        else:
            metrics = scan_directory(project_dir)

            all_lines += metrics[0]
            all_functions += metrics[1]
            all_files += metrics[2]
    print("=" * 60)
    print(f"Файлов во всех папках: {all_files}")
    print(f"Строк во всех файлах: {all_lines}")
    print(f"Функций во всех файлах: {all_functions}")