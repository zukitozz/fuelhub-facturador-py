"""
Lógica de negocio pura: formato, validación y cálculo, sin tocar disco ni red ni
base de datos. Nada de acá sabe que existe el SFS, SUNAT o la BD de la app —por
eso se puede mover, probar y reusar sin arrastrar ese contexto.

Lo que sí necesita I/O (leer la BD, escribir en DATA, hablar con el SFS o con
SUNAT) se queda en main.py: mezclarlo acá rompería el mecanismo con el que los
tests de pruebas/ reemplazan esas piezas (parchean atributos de main, y eso solo
funciona si la pieza vive en ese módulo).
"""
