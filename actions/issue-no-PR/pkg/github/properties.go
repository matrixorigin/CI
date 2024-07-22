package github

import "strconv"

type Properties []Property

type Property interface {
	Set(key, value string) error
	Get(key string) string

	ToString() string
}

type commonProperty struct {
	title     string
	file      string
	col       int
	endColumn int
	line      int
	endLine   int
}

func (c *commonProperty) Set(key, value string) error {
	switch key {
	case "title":
		c.title = value
	case "file":
		c.file = value
	case "col":
		colInt, err := strconv.Atoi(value)
		if err != nil {
			return err
		}
		c.col = colInt
	case "endColumn":
		endColumn, err := strconv.Atoi(value)
		if err != nil {
			return err
		}
		c.endColumn = endColumn
	case "line":
		line, err := strconv.Atoi(value)
		if err != nil {
			return err
		}
		c.line = line
	case "endLine":
		endLine, err := strconv.Atoi(value)
		if err != nil {
			return err
		}
		c.endLine = endLine
	}
	return nil
}

func (c *commonProperty) Get(key string) string {
	switch key {
	case "title":
		return c.title
	case "file":
		return c.file
	case "col":
		return strconv.Itoa(c.col)
	case "endColumn":
		return strconv.Itoa(c.endColumn)
	case "line":
		return strconv.Itoa(c.line)
	case "endLine":
		return strconv.Itoa(c.endLine)
	}
	return ""
}

func (c *commonProperty) ToString() string {
	str := ""
	if c.title != "" {
		str += ",title=" + c.title
	}
	if c.file != "" {
		str += ",file=" + c.file
	}

	if c.col != 0 {
		str += ",col=" + strconv.Itoa(c.col)
	}
	if c.endColumn != 0 {
		str += ",endColumn=" + strconv.Itoa(c.endColumn)
	}
	if c.line != 0 {
		str += ",line=" + strconv.Itoa(c.line)
	}
	if c.endLine != 0 {
		str += ",endLine=" + strconv.Itoa(c.endLine)
	}

	if len(str) == 0 {
		str = ","
	}

	return str[1:]
}
