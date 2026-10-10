# Первый запуск
make bootstrap

# Разработка
make check              # lint + mypy + tests
make test-watch         # автозапуск тестов

# Демонстрация
make demo               # сгенерировать все SVG

# Работа с конкретным потоком
make run FILE=stream.log BACKEND=tk

# Интеграция с Pascal
make patch PASCAL_SRC=graf.pas
fpc graf_patched.pas
./graf_patched &

make vuk PORT=7401 BACKEND=svg

# Очистка
make clean              # артефакты
make clean-all          # включая .venv