# Receipt convert

Converts images stored in PDFs found in the drive folder "Inbox" to images stored in "Slips"

Each image is scanned by OpenAI GPT-4o and a name for the file is generated. The name follows the following format:

`<merchant name>_<date of transaction>_<amount in zar>.jpg`

## Credentials
* OpenAI token which you need to export as `OPENAI_TOKEN`
* Google Credentials in a json file

## Run the program

```bash
uv run main
```
