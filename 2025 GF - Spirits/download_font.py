import requests
import os

os.makedirs("static", exist_ok=True)
url = "https://github.com/owent-utils/font/raw/master/fonts/noto/NotoSansSC-Regular.otf"
save_path = "./static/NotoSansSC-Regular.otf"

response = requests.get(url)
if response.status_code == 200:
    with open(save_path, "wb") as f:
        f.write(response.content)
    print("Font downloaded successfully to", save_path)
else:
    print("Failed to download! Status code:", response.status_code)
