import os
from dotenv import load_dotenv
load_dotenv()
from iptv_billing import create_app
app = create_app()
if __name__ == '__main__':
    app.run(port=5003, debug=True)
