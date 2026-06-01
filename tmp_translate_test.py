from deep_translator import GoogleTranslator
samples = [
    '你好世界',
    '这是中文测试。',
    '今天天气不错。',
]
with open('tmp_translate_output.txt', 'w', encoding='utf-8') as f:
    for s in samples:
        f.write(f'orig: {repr(s)}\n')
        for target in ['en', 'zh-CN', 'zh-TW']:
            try:
                translated = GoogleTranslator(source='auto', target=target).translate(s)
                f.write(f'-> {target}: {repr(translated)}\n')
            except Exception as e:
                f.write(f'-> {target} error: {repr(e)}\n')
        f.write('---\n')
