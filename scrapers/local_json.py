import base64
import zlib

b64_str = """
eJzNVt1rE0EQf/evGN/LMLO7s7sTsChYER8UJNBHic2aHBy50Fxt/e9d9b7SXBoLPevTsTAzO7+Pmdtz
mKcy7a6qbbFZwXydKvha3azWNZTFJsG3qiyr292Lc7hMcPnpI9TrBPX1YplewsVdurqp03LWZjC8BkIi
WKwWxWZXQ2RDQlBtoNrWRf5Ya2NUl8u9u3g7A4NCZ/Dmw+cZWCPI8Qzm7/OBrVckm8P28yDdbYvrtITb
ol7/Ofz48n1RvuJ8a44+gLKryuVjgPyO/wWDhzCMZzFhACN6DYF17ManI++BWxvyjLTkWULWljxHhIGn
o+NAVZcb8z6MqSoOOXSNWYfB/yeNCar0jAW0o670hK7r34pFMdP1zypk+aH+gwwa005xYyNG7celccqj
x+XpzOudRm8GUIKSskw4pKfEtzm4Jc8Jmo48DoIh9uQ1eafIa4r2M5iLetcVzQtsqEgD/2hROlJUFB13
RZUmF87mKfVxSKMhwzS66ybari4rIkpjwvk8mz3HqmjiP3RU1tA6/3wCHLhDPbLploDJ/8yB5RoaT1lu
H9sz/WEPh/c+6H4lR81LuANNsvdQaPL+cni7deoyd5H7dWqPGGvCp5GEYKOTtjnXu975gNQ9jUyMSP4n
j5zYXA==
"""

# Clean formatting and decode
clean_str = b64_str.replace("\n", "").replace(" ", "").strip()
decoded_bytes = base64.b64decode(clean_str)
decompressed_str = zlib.decompress(decoded_bytes).decode('utf-8')

# Split the pipe-separated log
events = decompressed_str.split('|')

print(f"✅ Successfully recovered {len(events)} events!\n")
for event in events:
    print(event)