![PIPSEEK Banner](https://raw.githubusercontent.com/ohmybahgosh/pipseek/refs/heads/main/PIPSEEK.png)

## A modern, feature-rich command-line tool for searching PyPI packages with beautiful formatting and real-time updates. Designed to replace the deprecated pip search functionality with an enhanced user experience.

## Attribution Notice

The proof-of-work challenge solution implementation in this tool is based on the work of [Long0x0](https://github.com/victorgarric/pip_search/issues/44#issuecomment-2565442916).
 
     

# Example Output 🖥️

[![asciicast](https://asciinema.org/a/C4kuX93qaywOVpj3n6dny6ufY.svg)](https://asciinema.org/a/C4kuX93qaywOVpj3n6dny6ufY)
 

## Why pipseek? 🤔

Since the deprecation of `pip search`, many developers have created alternative search tools. However, in late 2023, PyPI implemented proof-of-work challenges to prevent scraping abuse. This change broke most existing search tools and scripts that relied on directly scraping PyPI's search pages.

pipseek is specifically designed to handle PyPI's new proof-of-work challenge system while providing a superior search experience. It automatically solves these challenges in the background, ensuring reliable package search functionality even with PyPI's anti-scraping measures in place.

## Features ✨

- 🔍 Fast and efficient PyPI package search
- 🎨 Beautiful terminal output with rich formatting
- 📊 Detailed package information including version, author, and license
- ⚡️ Concurrent package information fetching
- 🔄 Real-time status updates
- 🛡️ Built-in handling of PyPI's proof-of-work challenges
- 📅 Sorted results by latest update time

## Installation & Setup 💻

1. Clone the repository:
```bash
git clone https://github.com/OhMyBahGosh/pipseek.git
cd pipseek
```

2. Install required dependencies:

```bash
pip install -r requirements.txt
```

For DEBIAN 12 based systems (Ubuntu 22.04+) if you encounter permission issues:
```bash
pip install --break-system-packages -r requirements.txt
```
⚠️ **CAUTION**: Using `--break-system-packages` can potentially interfere with your system's Python packages and is not recommended unless you understand the risks. This flag bypasses important system protections.

3. Make the script executable and install system-wide (Linux/Mac):
```bash
chmod +x pipseek

# Copy to /usr/local/bin (requires sudo)
sudo cp pipseek /usr/local/bin/pipseek

# Now you can run from anywhere using:
pipseek package-name
```

For development/testing, run directly:
```bash
# From the project directory
./pipseek package-name

# Or using Python explicitly
python3 pipseek package-name
```

Note: The script uses a Python shebang line, so it should not be executed directly with python.

The script will display detailed information for each package, including:
- Package name and current version
- Description
- Installation command
- Last update date
- Author information
- License details
- Homepage URL

## Dependencies 📚

Required Python packages:
- requests
- beautifulsoup4
- rich
- humanize
- packaging

## Features in Detail 🔍

### Rich Terminal Output
- Colorized and formatted output using the `rich` library
- Clear visual hierarchy of information
- Unicode icons for better visual organization

### Concurrent Processing
- Parallel fetching of package details
- Efficient handling of multiple package information requests
- Progress indication during searches

### PyPI Integration
- Handles PyPI's proof-of-work challenges automatically
- Reliable package information retrieval
- Comprehensive error handling

## Contributing 🤝

Contributions are welcome! Here's how you can help:

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License 📄

This project is licensed under the GNU General Public License v3.0 - see the LICENSE file for details.

## Credits 👏

Created with ❤️ by [OhMyBahGosh](https://github.com/OhMyBahGosh)

## Changelog 📝

### 1.0.0
- Initial release
- Basic search functionality
- Rich terminal output
- Concurrent package information fetching

---

Made with Python and lots of ☕
