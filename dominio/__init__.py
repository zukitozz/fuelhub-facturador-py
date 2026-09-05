"""
Lógica de negocio pura: formato, validación y cálculo, sin tocar disco ni red ni
base de datos. Nada de acá sabe que existe el SFS, SUNAT o la BD de la app —por
eso se puede mover, probar y reusar sin arrastrar ese contexto.

Lo que sí necesita I/O vive en su propio puerto+adaptador: la BD de la app en
repositorio/, el SFS local en sfs/, la consulta directa a SUNAT en sunat/, el
estado propio en disco en estado/, y la orquestación de todo eso en aplicacion/.
Los tests de pruebas/ parchean atributos del módulo donde la función que llaman
hace la búsqueda por nombre — el mismo mecanismo de siempre, apuntando a ese
módulo en vez de a main.py.
"""
