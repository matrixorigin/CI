import * as core from '@actions/core';
import * as tools from '@actions/tool-cache';
import * as fs from 'fs';
import * as path from 'path';
import { cmake } from '../src/cmake';
import { ninja } from '../src/ninja';

jest.mock('@actions/core');
jest.mock('@actions/tool-cache');
jest.mock('fs', () => ({
    ...jest.requireActual<typeof fs>('fs'),
    existsSync: jest.fn()
}));
jest.mock('child_process', () => ({
    ...jest.requireActual<typeof import('child_process')>('child_process'),
    spawnSync: jest.fn(() => ({ status: 0 }))
}));

const mockedCore = core as jest.Mocked<typeof core>;
const mockedTools = tools as jest.Mocked<typeof tools>;
const arch = Object.getOwnPropertyDescriptor(process, 'arch')!;
const originalTemp = process.env.RUNNER_TEMP;
let inputs: Record<string, string>;

beforeEach(() => {
    jest.resetAllMocks();
    Object.defineProperty(process, 'arch', { ...arch, value: 'arm64' });
    process.env.RUNNER_TEMP = '/runner/temp';
    inputs = { platform: 'mac', cmake: '3.17.2', ninja: '1.9.0' };
    mockedCore.getInput.mockImplementation(name => inputs[name] || '');
    mockedCore.group.mockImplementation(async (_name, callback) => callback());
    mockedTools.find.mockReturnValue('');
    mockedTools.downloadTool.mockResolvedValue('/download/archive');
    mockedTools.extractTar.mockResolvedValue('/extracted');
    mockedTools.extractZip.mockResolvedValue('/extracted');
    (fs.existsSync as jest.Mock).mockReturnValue(false);
});

afterEach(() => {
    Object.defineProperty(process, 'arch', arch);
    if (originalTemp === undefined) delete process.env.RUNNER_TEMP;
    else process.env.RUNNER_TEMP = originalTemp;
});

test.each(['mac', 'darwin'])(
    'CMake native cold install with %s alias',
    async platform => {
        inputs.platform = platform;
        const executable = await cmake();
        expect(mockedTools.downloadTool).toHaveBeenCalledWith(
            'https://github.com/Kitware/CMake/releases/download/v3.22.6/cmake-3.22.6-macos-universal.tar.gz'
        );
        const root = mockedTools.cacheDir.mock.calls[0][0];
        expect(mockedTools.cacheDir).toHaveBeenCalledWith(
            root,
            'cmake',
            '3.22.6'
        );
        expect(executable).toBe(
            path.join(
                root,
                'cmake-3.22.6-macos-universal/CMake.app/Contents/bin/cmake'
            )
        );
        expect(path.resolve(mockedCore.addPath.mock.calls[0][0])).toBe(
            path.dirname(executable)
        );
    }
);

test.each([
    [
        'mac',
        'arm64',
        '3.22.6',
        'macos-universal',
        'CMake.app/Contents/bin/cmake'
    ],
    ['linux', 'x64', '3.17.2', 'Linux-x86_64', 'bin/cmake'],
    ['mac', 'x64', '3.17.2', 'Darwin-x86_64', 'CMake.app/Contents/bin/cmake'],
    ['win', 'x64', '3.17.2', 'win64-x64', 'bin/cmake.exe']
])(
    'CMake %s/%s cache hit uses stored extraction layout',
    async (platform, cpu, version, suffix, binary) => {
        Object.defineProperty(process, 'arch', { ...arch, value: cpu });
        inputs.platform = platform;
        mockedTools.find.mockReturnValue('/cache/cmake');
        const executable = await cmake();
        expect(mockedTools.find).toHaveBeenCalledWith('cmake', version);
        expect(executable).toBe(
            path.join('/cache/cmake', `cmake-${version}-${suffix}`, binary)
        );
        expect(path.resolve(mockedCore.addPath.mock.calls[0][0])).toBe(
            path.dirname(executable)
        );
        expect(mockedTools.downloadTool).not.toHaveBeenCalled();
        expect(mockedTools.cacheDir).not.toHaveBeenCalled();
    }
);

test.each([
    ['3.22.5', '3.22.6'],
    ['3.22.6', '3.22.6'],
    ['3.31.6', '3.31.6']
])('CMake version boundary %s -> %s', async (requested, effective) => {
    inputs.cmake = requested;
    await cmake();
    expect(mockedTools.downloadTool).toHaveBeenCalledWith(
        `https://github.com/Kitware/CMake/releases/download/v${effective}/cmake-${effective}-macos-universal.tar.gz`
    );
});

test.each([
    ['linux', 'Linux-x86_64', '.tar.gz'],
    ['mac', 'Darwin-x86_64', '.tar.gz'],
    ['win', 'win64-x64', '.zip']
])(
    'CMake %s/x64 default URL unchanged',
    async (platform, suffix, extension) => {
        Object.defineProperty(process, 'arch', { ...arch, value: 'x64' });
        inputs.platform = platform;
        await cmake();
        expect(mockedTools.downloadTool).toHaveBeenCalledWith(
            `https://github.com/Kitware/CMake/releases/download/v3.17.2/cmake-3.17.2-${suffix}${extension}`
        );
    }
);

test.each([
    ['mac', 'arm64', '1.9.0', '1.12.1'],
    ['darwin', 'arm64', '1.12.0', '1.12.1'],
    ['mac', 'arm64', '1.12.1', '1.12.1'],
    ['mac', 'arm64', '1.13.1', '1.13.1'],
    ['mac', 'x64', '1.9.0', '1.9.0'],
    ['linux', 'x64', '1.9.0', '1.9.0'],
    ['win', 'x64', '1.9.0', '1.9.0']
])(
    'Ninja %s/%s version %s -> %s',
    async (platform, cpu, requested, effective) => {
        Object.defineProperty(process, 'arch', { ...arch, value: cpu });
        inputs.platform = platform;
        inputs.ninja = requested;
        const executable = await ninja();
        const assetPlatform = platform === 'darwin' ? 'mac' : platform;
        expect(mockedTools.downloadTool).toHaveBeenCalledWith(
            `https://github.com/ninja-build/ninja/releases/download/v${effective}/ninja-${assetPlatform}.zip`
        );
        expect(mockedTools.cacheFile).toHaveBeenCalledWith(
            executable,
            platform === 'win' ? 'ninja.exe' : 'ninja',
            'ninja',
            effective
        );
    }
);

test('Ninja cache stays flat and bypasses download', async () => {
    mockedTools.find.mockReturnValue('/cache/ninja');
    expect(await ninja()).toBe('/cache/ninja/ninja');
    expect(mockedCore.addPath).toHaveBeenCalledWith('/cache/ninja');
    expect(mockedTools.downloadTool).not.toHaveBeenCalled();
});

test.each([
    ['CMake download', cmake, 'downloadTool'],
    ['CMake extract', cmake, 'extractTar'],
    ['Ninja download', ninja, 'downloadTool'],
    ['Ninja extract', ninja, 'extractZip']
] as const)(
    '%s failure cannot publish PATH/cache',
    async (_label, install, operation) => {
        mockedTools[operation].mockRejectedValue(new Error('injected failure'));
        await expect(install()).rejects.toThrow('injected failure');
        expect(mockedCore.addPath).not.toHaveBeenCalled();
        expect(mockedTools.cacheDir).not.toHaveBeenCalled();
        expect(mockedTools.cacheFile).not.toHaveBeenCalled();
    }
);
