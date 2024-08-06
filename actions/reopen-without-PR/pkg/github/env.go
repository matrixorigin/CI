package github

import "os"

type envProperty struct {
	key   string
	value string
}

func NewEnvProperty(key, value string) Property {
	return &envProperty{
		key:   key,
		value: value,
	}
}

func (e *envProperty) Set(key, value string) error {
	e.key = key
	e.value = value
	return nil
}

func (e *envProperty) Get(key string) string {
	if key == e.key {
		return e.value
	}
	return ""
}

func (e *envProperty) ToString() string {
	return prepareKeyValueMessage(e.key, e.value)
}

// SetEnv you can make an environment variable available to any subsequent steps in a workflow job by defining or updating the environment variable
func SetEnv(properties Properties) error {
	envFile := os.Getenv("GITHUB_ENV")
	for i := 0; i < len(properties); i++ {
		err := writeCommandFile(envFile, properties[i].ToString())
		if err != nil {
			return err
		}
	}
	return nil
}
