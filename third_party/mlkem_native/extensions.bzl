# Copyright The mlkem-native project authors
# Copyright zeroRISC Inc.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

mlkem_native = module_extension(
    implementation = lambda _: _mlkem_native_repos(),
)

def _mlkem_native_repos():
    http_archive(
        name = "mlkem_native",
        build_file = Label("//third_party/mlkem_native:BUILD.mlkem_native.bazel"),
        sha256 = "e8ea95aa7e4fc597ae87a05624fa959dc57269e2e836428cf26370820c5339c4",
        strip_prefix = "mlkem-native-071fa87d3407930dcc3bc00ce23641cdc41c1278",
        urls = [
            "https://github.com/pq-code-package/mlkem-native/archive/071fa87d3407930dcc3bc00ce23641cdc41c1278.tar.gz",
        ],
    )
