# Rejected: precise trace, and trace itself

`trace` and the optional SCIP/LSP layer behind it are not shipping. Search already returns each hit under its enclosing symbol, and one `read` takes every symbol a question needs. A syntactic trace was noisy on common names (reference precision 0.035 against rust-analyzer on a ripgrep sample) and still had to be checked by reading the source. A precise layer would have added an index or a language server, which this tool set does not require. The measured syntactic baseline remains in the closed draft that proposed the layer.
