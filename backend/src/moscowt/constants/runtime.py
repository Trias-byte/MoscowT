SERVER_COMMAND = ("serve", "--host", "0.0.0.0", "--port", "8000")
PROCESS_COMMANDS = (
    SERVER_COMMAND,
    ("worker", "--channel", "general"),
    ("worker", "--channel", "model"),
)
PROCESS_POLL_SECONDS = 0.2
SHUTDOWN_TIMEOUT_SECONDS = 20
