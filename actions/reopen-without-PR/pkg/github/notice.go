package github

type noticeProperty struct {
	commonProperty
}

// Notice creates a notice message and prints the message to the log.
// This message will create an annotation, which can associate the message with a particular file in your repository.
// Optionally, your message can specify a position within the file by NewNoticeProperty.
func Notice(property Property, message string) error {
	return Run("notice", property, message)
}

// NewNoticeProperty create notice property. You can specify a position within the file.
func NewNoticeProperty(title string, file string, col int, endColumn, endLine int, line int) Property {
	return &noticeProperty{
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
