FROM docker.elastic.co/elasticsearch/elasticsearch:8.13.0

# Chinese analysis uses analysis-ik (ik_max_word for indexing, ik_smart for
# searching). The plugin version must match the ES version (8.13.0).
# ``service_backends.py`` resolves the analyzer at runtime (ik -> smartcn ->
# standard) so the index can still be created when no Chinese plugin is
# installed. To use smartcn instead, replace the RUN layer below with
# ``bin/elasticsearch-plugin install -b analysis-smartcn``.
RUN bin/elasticsearch-plugin install -b https://get.infini.cloud/elasticsearch/analysis-ik/8.13.0
