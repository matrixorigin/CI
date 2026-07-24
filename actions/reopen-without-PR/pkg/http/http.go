package http

import (
	"fmt"
	"io"
	"net/http"
	"time"
)

// 创建HTTP客户端并设置超时
var client = &http.Client{Timeout: 10 * time.Second}

func Request(method, url, token string, data io.Reader, contentType string) (resp *http.Response, err error) {
	req, err := http.NewRequest(method, url, data)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", token))
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}

	resp, err = client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Request failed: %v", err)
	}
	return resp, nil
}
