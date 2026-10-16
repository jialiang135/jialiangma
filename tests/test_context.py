"""Application-context composition and replacement are reversible."""


def test_app_context_defaults_and_explicit_provider_injection():
    from config.context import AppContext
    from config.providers import DashScopeProvider, DeepSeekChatProvider

    default = AppContext()
    assert isinstance(default.chat, DeepSeekChatProvider)
    assert isinstance(default.embed, DashScopeProvider)

    settings = object()
    chat = object()
    embed = object()
    injected = AppContext(settings=settings, chat=chat, embed=embed)
    assert injected.settings is settings
    assert injected.chat is chat
    assert injected.embed is embed


def test_context_can_be_replaced_and_reset():
    import config.context as context

    original = context.get_context()
    replacement = context.AppContext(chat=object(), embed=object())
    try:
        context.set_context(replacement)
        assert context.get_context() is replacement
    finally:
        context.reset_context()
    assert context.get_context() is not replacement
    assert context.get_context().chat is not original.chat
