def register(ctx):
    from .exporter import setup, command
    ctx.register_cli_command('usage-export', 'Export sanitized account usage (explicit network opt-in)', setup, command)
