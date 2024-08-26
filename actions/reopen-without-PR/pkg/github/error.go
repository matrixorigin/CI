package github

type errorProperty struct {
	commonProperty
}

// Error creates an error message and prints the message to the log.
// This message will create an annotation, which can associate the message with a particular file in your repository.
// Optionally, your message can specify a position within the file by NewErrorProperty.
func Error(property Property, message string) error {
	return Run("error", property, message)
}

func NewErrorProperty(title string, file string, col int, endColumn, endLine int, line int) Property {
	return &errorProperty{
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
