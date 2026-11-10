# Los agentes llaman a Claude a través de `claude -p` por defecto, no de la API

Para la demo, los agentes usan Claude Code en modo no interactivo (`claude -p`) con la suscripción del usuario, así no hace falta una API key ni facturación aparte. Es más lento y sirve para uso propio o demos, no para un producto ofrecido a terceros. Por eso el cliente de Claude es una interfaz con dos adaptadores (CLI y API de Anthropic), y pasar a la API es un cambio de configuración.
