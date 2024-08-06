package github

// StartGroup creates an expandable group in the log.
func StartGroup(title string) error {
	return Run("group", nil, title)
}

func StopGroup() error {
	return Run("endgroup", nil, "")
}
