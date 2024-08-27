package github

type warningProperty struct {
	commonProperty
}

// Warning creates a warning message and prints the message to the log.
// This message will create an annotation, which can associate the message with a particular file in your repository.
// Optionally, your message can specify a position within the file by NewWarningProperty.
// property can be nil if you don't need to specify a position.
func Warning(property Property, message string) error {
	return Run("warning", property, message)
}

// NewWarningProperty create warning property. You can specify a position within the file.
func NewWarningProperty(title string, file string, col int, endColumn, endLine int, line int) Property {
	return &warningProperty{
		commonProperty{
			title:     title,
			file:      file,
			col:       col,
			endColumn: endColumn,
			line:      line,
			endLine:   endLine,
		},
	}
}
